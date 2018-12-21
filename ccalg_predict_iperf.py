import boto3
import botocore
import os
import stat
import yaml
import time
import logging
import paramiko
import command
import cctestbedv2 as cctestbed
import cctestbed_generate_experiments as generate_experiments
from contextlib import contextmanager, ExitStack
import getpass
import glob
import json
import multiprocessing as mp
import pandas as pd
from data_analysis.experiment import untarfile
from data_analysis.experiment import Experiment
from datetime import datetime
import ccalg_predict
import traceback
import argparse

import rtt_exps

from logging.config import fileConfig
log_file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logging_config.ini')
fileConfig(log_file_path)    
logging.getLogger("paramiko").setLevel(logging.WARNING)

# CCALGS = ['cubic','reno','bbr']
CCALGS = ['cubic','reno','bbr', 'bic', 'cdg', 'highspeed', 'htcp', 'hybla', 'illinois', 'lp', 'nv', 'scalable', 'vegas', 'veno', 'westwood', 'yeah']

def get_all_regions():
    """Get all EC2 regions"""
    ec2 = boto3.client('ec2')
    response = ec2.describe_regions()
    regions = [region['RegionName'] for region in response['Regions']]
    return regions

def get_ec2(region_name=None):
    """Get a boto3 client for EC2 with givien region"""
    return boto3.client('ec2', region_name=region_name)

def region_has_instance(ec2):
    """Return True if this EC2 region has atleast one
    instance.
    """
    response = ec2.describe_instances()
    if len(response['Reservations']) > 0:
        return True
    return False

def get_instance(region):
    """Return instance object from this region. Assumes there is only one"""
    running_filter = [{'Name': 'instance-state-name',
                                 'Values': ['running']}]
    # could have also done boto3.resource('ec2', region_name=region)
    instances = list(boto3
                     .resource('ec2',region_name=region)
                     .instances
                     .filter(Filters=running_filter).all())
    if len(instances) == 0:
        return None
    assert(len(instances) == 1)
    instance = instances[0]
    return instance
    

def get_key_pair_path(ec2):
    """Get key pairs for this EC2 region"""
    # assume only one key pair per region and
    # keys are always stored in ~/.ssh/<KeyName>.pem
    key_pair_name = get_key_name(ec2)
    if key_pair_name is None:
        return None
    else:
        key_pair_path = '{}/.ssh/{}.pem'.format(os.environ['HOME'], key_pair_name)
        assert(os.path.isfile(key_pair_path))
        return key_pair_path

def get_key_name(ec2):
    response = ec2.describe_key_pairs()
    key_pairs = response['KeyPairs']
    if len(key_pairs) == 0:
        return None
    else:
        # key name must start with rware
        for key_pair in key_pairs:
            if key_pair['KeyName'].startswith('rware'):
                return key_pair['KeyName']
        return None
    
def create_key_pair(ec2, region_name):
    response = ec2.create_key_pair(KeyName='rware-{}'.format(region_name))
    key_pair_name = response['KeyName']
    key_pair_path = '{}/.ssh/{}.pem'.format(os.environ['HOME'], key_pair_name)
    with open(key_pair_path, 'w') as f:
        f.write(response['KeyMaterial'])
    os.chmod(key_pair_path, stat.S_IRUSR | stat.S_IWUSR)
    return key_pair_path

@contextmanager
def region_start_instance(ec2):
    instance =  _region_start_instance(ec2)
    try:
        yield instance
    finally:
        instance.terminate()
        
def _region_start_instance(ec2, image_id=None):
    """Start an EC2 instance in this region"""
    # find an availabilty zone
    all_zones = ec2.describe_availability_zones()
    available_zone = None
    region_name = None
    for zone in all_zones['AvailabilityZones']:
        if zone['State'] == 'available':
            available_zone = zone['ZoneName']
            region_name = zone['RegionName']
            break
    # force specifici availability zone us-west-1c
    # TODO: remove this hard coding and keep retrying zones
    # until succesful if there is an error
    if region_name == 'us-west-1':
        available_zone = 'us-west-1c'
    if region_name == 'ap-northeast-1':
        available_zone = 'ap-northeast-1c'
    if available_zone is None:
        raise RuntimeError('Could not find any available zones')
    # get key name
    key_name = get_key_name(ec2)
    if image_id is None:
        image_id = list(boto3
                        .resource('ec2', region_name=region_name)
                        .images
                        .filter(Filters=[{'Name':'name',
                                          'Values': ['ubuntu/images/hvm-ssd/ubuntu-xenial-16.04-amd64-server-201806*']}])
                        .all())[0].id
    assert(key_name is not None)
    # create 1 ubuntu t2.micro instance
    instance = boto3.resource('ec2', region_name=region_name).create_instances(
        ImageId=image_id,
        InstanceType='t2.micro',
        Placement={
            'AvailabilityZone':available_zone},
        KeyName=key_name,
        NetworkInterfaces=[
            {'AssociatePublicIpAddress':True,
             'DeviceIndex':0}],
        MaxCount=1,
        MinCount=1)

    ssh_allow_rule = {'FromPort': 22,
                      'IpProtocol': 'tcp',
                      'IpRanges': [{'CidrIp':'0.0.0.0/0'}],
                      'Ipv6Ranges':[],
                      'PrefixListIds':[],
                      'ToPort': 22,
                      'UserIdGroupPairs': []}
    try:
        response = ec2.authorize_security_group_ingress(GroupName='default',
                                                        IpPermissions=[ssh_allow_rule])
    except botocore.exceptions.ClientError as e:
        if not (e.response['Error']['Code'] == 'InvalidPermission.Duplicate'):
            raise e
        
    return instance[0]

def clone_cctestbed(ec2, instance, git_secret, ec2_username='ubuntu'):
    key_pair_path = get_key_pair_path(ec2)
    cmd = ('cd /opt '
           '&& sudo chown -R ubuntu /opt '
           '&& git clone git@github.com:rware/cctestbed.git ')
    cmd = ('cd /opt'
           '&& sudo chown -R ubuntu /opt '
           '&& git clone https://rware:{}@github.com/rware/cctestbed.git').format(git_secret)
    with command.get_ssh_client(ip_addr=instance.public_ip_address,
                                username=ec2_username,
                                key_filename=key_pair_path) as ssh_client:
        session = ssh_client.get_transport().open_session()
        #paramiko.agent.AgentRequestHandler(session)
        session.set_combine_stderr(True)
        stdout = session.makefile()
        try:
            logging.info('Running cmd ({}): {}'.format(
                instance.public_ip_address, cmd.replace(git_secret, '****')))
            session.exec_command(cmd)
            exit_status =  session.recv_exit_status()
            return exit_status, stdout.read()
        except:
            stdout.close()

def run_ec2_command(ec2, instance, cmd, ec2_username='ubuntu'):
    key_pair_path = get_key_pair_path(ec2)
    with command.get_ssh_client(ip_addr=instance.public_ip_address,
                                username=ec2_username,
                                key_filename=key_pair_path) as ssh_client:
        _, stdout, stderr = command.exec_command(ssh_client,
                                                 instance.public_ip_address,
                                                 cmd)
        # actually should return a bad exit status
        exit_status =  stdout.channel.recv_exit_status()
        return exit_status, stdout.read()


def update_kernel(ec2, instance, ec2_username='ubuntu'):
    cmd = ('cd /opt/cctestbed '
           '&& ./setup-kernel.sh upgrade_kernel ')
    return run_ec2_command(ec2, instance, cmd, ec2_username)    
    
def install_iperf3(ec2, instance, ec2_username='ubuntu'):
    cmd = ('cd /opt/cctestbed '
           '&& ./setup-kernel.sh install_iperf3 ')
    return run_ec2_command(ec2, instance, cmd, ec2_username)

def wait_for_ssh(ec2, instance, ec2_username='ubuntu'):
    while True:
        try:
            with command.get_ssh_client(ip_addr=instance.public_ip_address,
                                        username=ec2_username,
                                        key_filename=get_key_pair_path(ec2)) as ssh_client:
                _, stdout, stderr = command.exec_command(ssh_client,
                                                         instance.public_ip_address,
                                                         'echo "TESTING SSH CONNECTION"')
                break
        except:
            logging.info('Waiting 60s for machine to boot')
            time.sleep(60)


def setup_ec2(ec2, instance, git_secret, ec2_username='ubuntu'):
    wait_for_ssh(ec2, instance)
    logging.info('Cloning cctestbed')
    exit_status, stdout = clone_cctestbed(ec2, instance, git_secret, ec2_username)
    logging.info(stdout)
    logging.info('Updating kernel')
    exit_status, stdout = update_kernel(ec2, instance, ec2_username)
    logging.info(stdout)
    # make sure machine has time to reboot
    logging.info('Waiting 60s for machine to reboot')    
    time.sleep(60)
    wait_for_ssh(ec2, instance)
    exit_status, stdout = install_iperf3(ec2, instance, ec2_username)
    logging.info(stdout)
    cmds = [
    'cd /opt/cctestbed/tcp_bbr_measure && make',
    'echo net.core.wmem_max = 16777216 | sudo tee -a /etc/sysctl.conf',
    'echo net.core.rmem_max = 16777216 | sudo tee -a /etc/sysctl.conf',
    'echo net.core.wmem_default = 16777216 | sudo tee -a /etc/sysctl.conf', 
    'echo net.core.rmem_default = 16777216 | sudo tee -a /etc/sysctl.conf',
    'echo net.ipv4.tcp_wmem = 10240 16777216 16777216 | sudo tee -a /etc/sysctl.conf',
    'net.ipv4.tcp_rmem = 10240 16777216 16777216 | sudo tee -a /etc/sysctl.conf',
    'sudo sysctl -p'
    ]
    for cmd in cmds:
        exit_status, stdout = run_ec2_command(ec2, instance, cmd)
        logging.info(stdout)    

def install_kernel_modules(ec2, instance, ec2_username='ubuntu'):

    cmds = [
        'cd /opt/cctestbed/tcp_bbr_measure && sudo insmod tcp_probe_ray.ko',
        'for f in /lib/modules/$(uname -r)/kernel/net/ipv4/tcp_*; do sudo modprobe $(basename $f .ko); done',
        'sudo rmmod tcp_probe',
        "echo 'net.ipv4.tcp_allowed_congestion_control=cubic reno bic bbr cdg dctcp highspeed htcp hybla illinois lp nv scalable vegas veno westwood yeah' | sudo tee -a /etc/sysctl.conf",
        'sudo sysctl -p',
        'sudo ethtool -K eth0 tx off sg off tso off'
    ]
    for cmd in cmds:
        exit_status, stdout = run_ec2_command(ec2, instance, cmd, ec2_username)
        logging.info(stdout)    

def get_ec2_experiments(instance, ec2, region):
    server = generate_experiments.HOST_POTATO
    client = generate_experiments.HOST_AWS_TEMPLATE
    client['ip_wan'] = instance.public_ip_address
    client['ip_lan'] = instance.private_ip_address
    client['key_filename'] = get_key_pair_path(ec2)
    # create config and output
    config = generate_experiments.all_ccalgs_config(
        server, client,
        btlbw=10,
        rtt=1,
        end_time=60,
        exp_name_suffix=region.replace('-',''),
        queue_sizes=[32, 64, 128, 256, 512, 1024, 2048])
        #queue_sizes=[32, 64, 128, 256, 512])
    config_filename = 'experiments-all-ccalgs-aws-{}.yaml'.format(
        region.replace('-',''))
    logging.info('Writing config file {}'.format(config_filename))
    with open(config_filename, 'w') as f:
        yaml.dump(config, f, default_flow_style=False)
    experiments = cctestbed.load_experiments(config,
                                             config_filename, force=True)
    return experiments


# for aws experiments use icmp ping
def get_ping_rtt(instance_ip):
    #cmd = "nping --icmp -v-1 -H -c 5 {} | grep -oP 'Avg rtt:\s+\K.*(?=ms)'".format(instance_ip)
    cmd = 'ping -c 5 {} | tail -1 | awk "{{print $4}}" '.format(instance_ip),
    line = cctestbed.run_local_command(cmd, shell=True)
    rtt = float(line.split('=')[-1].split('/')[1])
    return rtt

            
def run_ec2_experiment(ec2, instance, ccalg, btlbw, rtt,
                       queue_size, region, force=False):
    experiment_name = '{}-{}bw-{}rtt-{}q-{}'.format(
        ccalg, btlbw, rtt, queue_size, region)
    if not force and ccalg_predict.is_completed_experiment(experiment_name):
        return
    else:
        pass
        #if ccalg_predict.ran_experiment_today(experiment_name):
        #    return
    logging.info('Creating experiment for instance: {}-{}'.format(region, ccalg))
    instance_rtt = int(float(get_ping_rtt(instance.public_ip_address)))
    logging.info('Got instance RTT: {}'.format(instance_rtt))

    if instance_rtt >= rtt:
        logging.warning('Skipping experiment with instance RTT {} >= {}'.format(
            instance_rtt, rtt))
        return -1

    server = generate_experiments.HOST_SERVER
    client = generate_experiments.HOST_AWS_TEMPLATE
    client['ip_wan'] = instance.public_ip_address
    client['ip_lan'] = instance.private_ip_address
    client['key_filename'] = get_key_pair_path(ec2)
    
    server_nat_ip = generate_experiments.HOST_CLIENT.ip_wan

    client = cctestbed.Host(**client)
    
    server_port = 5201
    client_port = 5555

    #print('Connecting dpdk')
    #cctestbed.connect_dpdk(server, client)

    flow = {'ccalg': ccalg,
            'end_time': 60,
            'rtt': rtt - instance_rtt,
            'start_time': 0}
    flows = [cctestbed.Flow(ccalg=flow['ccalg'], start_time=flow['start_time'],
                      end_time=flow['end_time'], rtt=flow['rtt'],
                      server_port=server_port, client_port=client_port,
                      client_log=None, server_log=None)]
    
    exp = cctestbed.Experiment(name=experiment_name,
                               btlbw=btlbw,
                               queue_size=queue_size,
                               flows=flows, server=server, client=client,
                               config_filename='experiments-all-ccalgs-aws.yaml',
                               server_nat_ip=server_nat_ip)
    
    try:
        # make sure old stuff closed
        exp.cleanup_last_experiment(cleanup_tail=False)
        logging.info('Running experiment: {}'.format(exp.name))
        with ExitStack() as stack:
            # add DNAT rule
            stack.enter_context(ccalg_predict.add_dnat_rule(exp, exp.client.ip_wan))
            # add route to URL
            stack.enter_context(ccalg_predict.add_route(exp, exp.client.ip_wan,
                                gateway_ip=generate_experiments.HOST_CLIENT.ip_lan))
            exp._run_tcpdump('server', stack)
            exp._run_tcpdump('client', stack)
            exp._run_tcpprobe(stack)
            stack.enter_context(exp._run_rtt_monitor(program='ping'))
            exp._run_all_flows(stack, bess_config_name='active-middlebox-pmd')
        # compress all log files
        proc = exp._compress_logs_url()
        logging.info('Finished experiment: {}'.format(exp.name))
        return proc
    except Exception as e:
        logging.error('Error occurred while running experiment '+exp.name)
        exp._delete_logs(delete_description=False)
        raise e

            
def get_region_image(region):
    aws_images = list(boto3
                      .resource('ec2', region_name=region)
                      .images
                      .filter(Filters=[{'Name':'name', 'Values':[region]}],
                              Owners=['self'])
                      .all())
    if len(aws_images) == 0:
        return None
    assert(len(aws_images) == 1)
    return aws_images[0]            
    
def get_taro_experiments(networks=None, force=True, loss=None, ccalgs=CCALGS):    
    if networks is None:
        ntwrk_conditions = [(5,35,16), (5,85,64), (5,130,64), (5,275,128),
                            (10,35,32), (10,85,128), (10,130,128), (10,275,256),
                            (15,35,64), (15,85,128), (15,130,256), (15,275,512)]

    else:
        ntwrk_conditions = networks

    experiments = {}
    for btlbw, rtt, queue_size in ntwrk_conditions:
            rtts = [rtt]
            config = generate_experiments.ccalg_predict_config(
                btlbw=btlbw,
                rtts=rtts,
                end_time=60,
                exp_name_suffix='local',
                queue_sizes=[queue_size],
                ccalgs=ccalgs,
                loss=loss)
            config_filename = 'experiments-ccalg-predict-{}bw-{}rtt-{}q-{}.yaml'.format(
                btlbw,
                rtt,
                queue_size,
                datetime.now().strftime('%Y%m%d'))
            logging.info('Writing config file {}'.format(config_filename))
            with open(config_filename, 'w') as f:
                yaml.dump(config, f, default_flow_style=False)
            experiments.update(cctestbed.load_experiments(config,
                                                          config_filename, force=force))
    return experiments

    
def run_local_exps(networks, force, loss, ccalgs):
    experiments = get_taro_experiments(networks, force, loss, ccalgs)
    completed_experiment_procs = []
    logging.info('Going to run {} experiments.'.format(len(experiments)))
    num_experiments = len(experiments.values())
    current_experiment = 1
    for experiment in experiments.values():
        print('Running experiment {}/{} -> {}'.format(
            current_experiment, num_experiments, experiment.name))
        proc = experiment.run(compress_logs_url=True)
        completed_experiment_procs.append(proc)
        current_experiment += 1
    for proc in completed_experiment_procs:
        logging.info('Waiting for subprocess to finish PID={}'.format(proc.pid))
        proc.wait()
        if proc.returncode != 0:
            logging.warning('Error running cmd PID={}'.format(proc.pid))

def run_aws_exps(force_create_instance=False, regions=None, networks=None, ccalgs=None, force=False):
    #regions = ['ap-south-1', 'eu-west-1']
    skip_regions = [] #['ap-south-1','eu-west-3','eu-west-1','eu-west-2','ap-northeast-2','ap-northeast-1']
    if regions is None:
        regions=get_all_regions()
    
    #regions = [
    #    'ap-northeast-1', 'ap-northeast-2', 'sa-east-1','ap-southeast-1','ap-southeast-2',
    #    'eu-central-1', 'us-east-1','us-east-2','us-west-1', 'ca-central-1', 'eu-west-3',
    #    'eu-west-2', 'us-west-2', 'ap-south-1','eu-west-1'] 

    if networks is None:
        ntwrk_conditions = [(5,35,16), (5,85,64), (5,130,64), (5,275,128),
                            (10,35,32), (10,85,128), (10,130,128), (10,275,256),
                            (15,35,64), (15,85,128), (15,130,256), (15,275,512)]
    else:
        ntwrk_conditions = networks

    if ccalgs is None:
        ccalgs = CCALGS

    num_exps = len(regions) * len(ntwrk_conditions) * len(ccalgs)
        
    logging.info('Found {} regions: {}'.format(len(regions), regions))
    # TODO: wait for all created images to be created
    created_images = []
    num_completed_regions = 0
    for region in regions:
        if region in skip_regions:
            logging.warning('Skipping region {}'.format(region))
            continue
        if not force_create_instance:
            instance = get_instance(region)
        else:
            instance = None
        ec2_region = get_ec2(region)
        if (instance is None) or (force_create_instance):
            if get_key_name(ec2_region) is None:
                logging.warning('Creating key pair for region {}'.format(region))
                create_key_pair(ec2_region, region)        
            image = get_region_image(region)
            if image is None:
                image_id = None
            else:
                image_id = image.id
            logging.info('Creating instance for region {}'.format(region))
            instance = _region_start_instance(ec2_region, image_id)
            try:
                instance.wait_until_running()
                instance.load()
                if image is None:
                    #logging.info('Setting up cctestbed on instance')
                    #setup_ec2(ec2_region, instance, git_secret, ec2_username='ubuntu')
                    logging.error('Image not setup for this region')
                    raise ValueError('Image not setup for this region')
            except Exception as e:
                instance.stop()
                raise e
        wait_for_ssh(ec2_region, instance, ec2_username='ubuntu')
        # need to install kernel modules every time
        install_kernel_modules(ec2_region, instance, ec2_username='ubuntu')
        try:
            completed_experiment_procs = []
            num_completed_exps = 0
            too_small_rtt = 0
            for btlbw, rtt, queue_size in ntwrk_conditions:
                for ccalg in ccalgs:
                    num_completed_exps += 1
                    if rtt <= too_small_rtt:
                        print('Skipping experiment RTT too small')
                        num_completed_exps += (len(CCALGS)-1)
                        break
                    print('Running experiment {}/{} region={}, ccalg={}, btlbw={}, rtt={}, queue_size={}'.format(num_completed_exps, num_exps, region, ccalg, btlbw, rtt, queue_size))
                    proc = run_ec2_experiment(ec2_region, instance, ccalg, btlbw, rtt,
                                              queue_size, region, force=force)
                    if proc == -1:
                        too_small_rtt = max(too_small_rtt, rtt)
                    elif proc is not None:
                        completed_experiment_procs.append(proc)
        except Exception as e:
            logging.error('Error running experiment for instance: {}-{}'.format(region, ccalg))
            logging.error(e)
            logging.error(traceback.print_exc())
            print('Error running experiment for instance: {}-{}'.format(region, ccalg))
            print(e)
            print(traceback.print_exc())
            raise e
        finally:
            for proc in completed_experiment_procs:
                logging.info('Waiting for subprocess to finish PID={}'.format(proc.pid))
                proc.wait()
                if proc.returncode != 0:
                    logging.warning('Error running cmd PID={}'.format(proc.pid))

            logging.info('Stopping instance')
            instance.stop()
            wait_time = 0
            while (instance.state['Name'] != 'stopped' and wait_time < 300):
                time.sleep(5)
                wait_time += 5
                instance.load()
            if get_region_image(region) is None:
                # create ec2 image before terminating
                logging.info('Creating image for region {}'.format(region))
                try:
                    instance.create_image(Name=region)
                except Exception as e:
                    logging.error('Error while trying to create image: {}', e)
        num_completed_regions += 1
        print('Completed experiments for {}/{} regions'.format(num_completed_regions, len(regions)))

def parse_args():
    parser = argparse.ArgumentParser(description='Run controlled iperf3 experiment')
    parser.add_argument('--regions','-r',
                        required=False, nargs='+', default=get_all_regions(),
                        help='AWS regions to perform experiment. Default is all 15 AWS regions')
    parser.add_argument('--network', '-n', nargs=3,
                        action='append', metavar=('BTLBW','RTT', 'QUEUE_SIZE'),
                        dest='networks', type=int, required=False,
                        default=[],
                        help='Network conditions to use for experiments')
    parser.add_argument('--ccalgs', '-c',
                        nargs='+',
                        default=CCALGS,
                        help='Congestion control algs')
    parser.add_argument('--force','-f', action='store_true',
                        help='Force experiments that were already run to run again')
    parser.add_argument('--loss','-l', nargs='+', required=False, type=float, default=None)
    args = parser.parse_args()
    return args

if __name__ == '__main__':
    args = parse_args()
    if args.networks == []:
        args.networks = [(5,35,16), (5,85,64), (5,130,64), (5,275,128),
                        (10,35,32), (10,85,128), (10,130,128), (10,275,256),
                         (15,35,64), (15,85,128), (15,130,256), (15,275,512)]
        
    if 'local' in args.regions:
        #args.networks = rtt_exps.ntwrk_conditions['bess-3']
        run_local_exps(args.networks, args.force, args.loss, args.ccalgs)
    else:
        #git_secret = getpass.getpass('Github secret: ')
        run_aws_exps(force_create_instance=True,
                     regions=args.regions,
                     networks=args.networks,
                     ccalgs=args.ccalgs,
                     force=args.force)

