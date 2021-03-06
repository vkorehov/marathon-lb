#!/usr/bin/env python3

from common import *
from datetime import datetime
from io import StringIO

import argparse
import json
import requests
import csv
import time
import re
import math
import socket
import urllib


logger = logging.getLogger('bluegreen_deploy')


def query_yes_no(question, default="yes"):
    # Thanks stackoverflow:
    # https://stackoverflow.com/questions/3041986/python-command-line-yes-no-input
    """Ask a yes/no question via input() and return their answer.

    "question" is a string that is presented to the user.
    "default" is the presumed answer if the user just hits <Enter>.
        It must be "yes" (the default), "no" or None (meaning
        an answer is required of the user).

    The "answer" return value is True for "yes" or False for "no".
    """
    valid = {"yes": True, "y": True, "ye": True,
             "no": False, "n": False}
    if default is None:
        prompt = " [y/n] "
    elif default == "yes":
        prompt = " [Y/n] "
    elif default == "no":
        prompt = " [y/N] "
    else:
        raise ValueError("invalid default answer: '%s'" % default)

    while True:
        sys.stdout.write(question + prompt)
        choice = input().lower()
        if default is not None and choice == '':
            return valid[default]
        elif choice in valid:
            return valid[choice]
        else:
            sys.stdout.write("Please respond with 'yes' or 'no' "
                             "(or 'y' or 'n').\n")


def get_app_info(args, deployment_group, alt_port):
    url = args.marathon + "/v2/apps"
    response = requests.get(url, auth=get_marathon_auth_params(args))
    response.raise_for_status()
    apps = response.json()
    existing_app = None
    colour = 'blue'
    next_port = alt_port
    resuming = False
    for app in apps['apps']:
        if 'labels' in app and \
                'HAPROXY_DEPLOYMENT_GROUP' in app['labels'] and \
                'HAPROXY_DEPLOYMENT_COLOUR' in app['labels'] and \
                app['labels']['HAPROXY_DEPLOYMENT_GROUP'] == deployment_group:
            if existing_app is not None:
                if args.resume:
                    logger.info("Found previous deployment, resuming")
                    resuming = True
                    if existing_app['labels']['HAPROXY_DEPLOYMENT_STARTED_AT']\
                            < app['labels']['HAPROXY_DEPLOYMENT_STARTED_AT']:
                        # stop here
                        break
                else:
                    raise Exception("There appears to be an existing"
                                    " deployment in progress")

            prev_colour = app['labels']['HAPROXY_DEPLOYMENT_COLOUR']
            prev_port = app['ports'][0]
            existing_app = app
            if prev_port == int(alt_port):
                next_port = app['labels']['HAPROXY_0_PORT']
            else:
                next_port = alt_port
            if prev_colour == 'blue':
                colour = 'green'
            else:
                colour = 'blue'
    return (colour, next_port, existing_app, resuming)


def get_hostports_from_backends(hmap, backends, haproxy_instance_count):
    hostports = {}
    regex = re.compile(r"^(\d+)_(\d+)_(\d+)_(\d+)_(\d+)$", re.IGNORECASE)
    counts = {}
    for backend in backends:
        svname = backend[hmap['svname']]
        if svname in counts:
            counts[svname] += 1
        else:
            counts[svname] = 1
        # Are all backends across all instances draining?
        if counts[svname] == haproxy_instance_count:
            m = regex.match(svname)
            host = '.'.join(m.group(1, 2, 3, 4))
            port = m.group(5)
            if host in hostports:
                hostports[host].append(int(port))
            else:
                hostports[host] = [int(port)]
    return hostports


def find_tasks_to_kill(tasks, hostports):
    tasks_to_kill = set()
    for task in tasks:
        if task['host'] in hostports:
            for port in hostports[task['host']]:
                if port in task['ports']:
                    tasks_to_kill.add(task['id'])
    return list(tasks_to_kill)


def check_if_tasks_drained(args, app, existing_app):
    time.sleep(args.step_delay)
    url = args.marathon + "/v2/apps" + existing_app['id']
    response = requests.get(url, auth=get_marathon_auth_params(args))
    response.raise_for_status()
    existing_app = response.json()['app']

    url = args.marathon + "/v2/apps" + app['id']
    response = requests.get(url, auth=get_marathon_auth_params(args))
    response.raise_for_status()
    app = response.json()['app']

    target_instances = \
        int(app['labels']['HAPROXY_DEPLOYMENT_TARGET_INSTANCES'])

    logger.info("Existing app running {} instances, "
                "new app running {} instances"
                .format(existing_app['instances'], app['instances']))

    url = args.marathon_lb
    url = urllib.parse.urlparse(url)
    # Have to find _all_ haproxy stats backends
    addrs = socket.gethostbyname_ex(url.hostname)[2]
    csv_data = ''
    for addr in addrs:
        try:
            nexturl = \
                urllib.parse.urlunparse((url[0],
                                         addr + ":" + str(url.port),
                                         url[2],
                                         url[3],
                                         url[4],
                                         url[5]))
            response = requests.get(nexturl + "/haproxy?stats;csv")
            response.raise_for_status()
            csv_data = csv_data + response.text

            response = requests.get(nexturl + "/_haproxy_getpids")
            response.raise_for_status()
            pids = response.text.split()
            if len(pids) > 1:
                # HAProxy has not finished reloading
                logger.info("Waiting for {} pids on {}"
                            .format(len(pids), nexturl))
                return check_if_tasks_drained(args,
                                              app,
                                              existing_app)
        except requests.exceptions.RequestException as e:
            logger.exception("Caught exception when retrieving HAProxy"
                             " stats from " + nexturl)
            return check_if_tasks_drained(args,
                                          app,
                                          existing_app)

    backends = []
    f = StringIO(csv_data)
    header = None
    haproxy_instance_count = 0
    for row in csv.reader(f, delimiter=',', quotechar="'"):
        if row[0][0] == '#':
            header = row
            haproxy_instance_count += 1
            continue
        if row[0] == app['labels']['HAPROXY_DEPLOYMENT_GROUP'] + "_" + \
                app['labels']['HAPROXY_0_PORT'] and \
                row[1] != "BACKEND" and \
                row[1] != "FRONTEND":
            backends.append(row)

    logger.info("Found {} app backends across {} HAProxy instances"
                .format(len(backends), haproxy_instance_count))
    # Create map of column names to idx
    hmap = {}
    for i in range(0, len(header)):
        hmap[header[i]] = i

    if (len(backends) / haproxy_instance_count) != \
            app['instances'] + existing_app['instances']:
        # HAProxy hasn't updated yet, try again
        return check_if_tasks_drained(args,
                                      app,
                                      existing_app)

    up_backends = \
        [b for b in backends if b[hmap['status']] == 'UP']
    if (len(up_backends) / haproxy_instance_count) < target_instances:
        # Wait until we're in a healthy state
        return check_if_tasks_drained(args,
                                      app,
                                      existing_app)

    # Double check that current draining backends are finished serving requests
    draining_backends = \
        [b for b in backends if b[hmap['status']] == 'MAINT']

    if (len(draining_backends) / haproxy_instance_count) < 1:
        # No backends have started draining yet
        return check_if_tasks_drained(args,
                                      app,
                                      existing_app)

    for backend in draining_backends:
        # Verify that the backends have no sessions or pending connections.
        # This is likely overkill, but we'll do it anyway to be safe.
        if int(backend[hmap['qcur']]) > 0 or int(backend[hmap['scur']]) > 0:
            # Backends are not yet drained.
            return check_if_tasks_drained(args,
                                          app,
                                          existing_app)

    # If we made it here, all the backends are drained and we can start
    # slaughtering tasks, with prejudice
    hostports = get_hostports_from_backends(hmap,
                                            draining_backends,
                                            haproxy_instance_count)

    tasks_to_kill = find_tasks_to_kill(existing_app['tasks'], hostports)

    logger.info("There are {} drained backends, "
                "about to kill & scale for these tasks:\n{}"
                .format(len(tasks_to_kill), "\n".join(tasks_to_kill)))

    if app['instances'] == target_instances and \
            len(tasks_to_kill) == existing_app['instances']:
        logger.info("About to delete old app {}".format(existing_app['id']))
        if args.force or query_yes_no("Continue?"):
            url = args.marathon + "/v2/apps" + existing_app['id']
            response = requests.delete(url,
                                       auth=get_marathon_auth_params(args))
            response.raise_for_status()
            return True
        else:
            return False

    if args.force or query_yes_no("Continue?"):
        # Scale new app up
        instances = math.floor(app['instances'] + (app['instances'] + 1) / 2)
        if instances >= existing_app['instances']:
            instances = target_instances
        logger.info("Scaling new app up to {} instances".format(instances))
        url = args.marathon + "/v2/apps" + app['id']
        data = json.dumps({'instances': instances})
        headers = {'Content-Type': 'application/json'}
        response = requests.put(url, headers=headers, data=data,
                                auth=get_marathon_auth_params(args))
        response.raise_for_status()

        # Scale old app down
        logger.info("Scaling down old app by {} instances"
                    .format(len(tasks_to_kill)))
        data = json.dumps({'ids': tasks_to_kill})
        url = args.marathon + "/v2/tasks/delete?scale=true"
        response = requests.post(url, headers=headers, data=data,
                                 auth=get_marathon_auth_params(args))
        response.raise_for_status()

        return check_if_tasks_drained(args,
                                      app,
                                      existing_app)
    return False


def start_deployment(args, app, existing_app, resuming):
    if not resuming:
        url = args.marathon + "/v2/apps"
        data = json.dumps(app)
        headers = {'Content-Type': 'application/json'}
        response = requests.post(url, headers=headers, data=data,
                                 auth=get_marathon_auth_params(args))
        response.raise_for_status()
    if existing_app is not None:
        return check_if_tasks_drained(args,
                                      app,
                                      existing_app)
    return False


def get_service_port(app):
    if 'container' in app and \
            'docker' in app['container'] and \
            'portMappings' in app['container']['docker']:
        portMappings = app['container']['docker']['portMappings']
        # Just take the first servicePort
        return portMappings[0]['servicePort']
    return app['ports'][0]


def set_service_port(app, port):
    if 'container' in app and \
            'docker' in app['container'] and \
            'portMappings' in app['container']['docker']:
        app['container']['docker']['portMappings'][0]['servicePort'] = \
            int(port)
        return app
    app['ports'][0] = int(servicePort)
    return app


def process_json(args, out=sys.stdout):
    with open(args.json, 'r') as content_file:
        content = content_file.read()

    app = json.loads(content)

    app_id = app['id']
    if app_id is None:
        raise Exception("App doesn't contain a valid App ID")

    if 'labels' not in app:
        raise Exception("No labels found. Please define the"
                        "HAPROXY_DEPLOYMENT_GROUP label"
                        )
    if 'HAPROXY_DEPLOYMENT_GROUP' not in app['labels']:
        raise Exception("Please define the "
                        "HAPROXY_DEPLOYMENT_GROUP label"
                        )
    if 'HAPROXY_DEPLOYMENT_ALT_PORT' not in app['labels']:
        raise Exception("Please define the "
                        "HAPROXY_DEPLOYMENT_ALT_PORT label"
                        )

    deployment_group = app['labels']['HAPROXY_DEPLOYMENT_GROUP']
    alt_port = app['labels']['HAPROXY_DEPLOYMENT_ALT_PORT']
    app['labels']['HAPROXY_APP_ID'] = app_id

    service_port = get_service_port(app)

    (colour, port, existing_app, resuming) = \
        get_app_info(args, deployment_group, alt_port)

    app = set_service_port(app, port)

    app['id'] = app_id + "-" + colour
    if app['id'][0] != '/':
        app['id'] = '/' + app['id']
    if existing_app is not None:
        app['instances'] = args.initial_instances
        app['labels']['HAPROXY_DEPLOYMENT_TARGET_INSTANCES'] = \
            str(existing_app['instances'])
    else:
        app['labels']['HAPROXY_DEPLOYMENT_TARGET_INSTANCES'] = \
            str(app['instances'])
    app['labels']['HAPROXY_DEPLOYMENT_COLOUR'] = colour
    app['labels']['HAPROXY_DEPLOYMENT_STARTED_AT'] = datetime.now().isoformat()
    app['labels']['HAPROXY_0_PORT'] = str(service_port)

    logger.info('Final app definition:')
    out.write(json.dumps(app, sort_keys=True, indent=2))
    out.write("\n")

    if args.dry_run:
        return

    if args.force or query_yes_no("Continue with deployment?"):
        start_deployment(args, app, existing_app, resuming)


def get_arg_parser():
    parser = argparse.ArgumentParser(
        description="Marathon HAProxy Load Balancer",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--longhelp",
                        help="Print out configuration details",
                        action="store_true"
                        )
    parser.add_argument("--marathon", "-m",
                        help="[required] Marathon endpoint, eg. -m " +
                             "http://marathon1:8080"
                        )
    parser.add_argument("--marathon-lb", "-l",
                        help="[required] Marathon-lb stats endpoint, eg. -l " +
                             "http://marathon-lb.marathon.mesos:9090"
                        )

    parser.add_argument("--json", "-j",
                        help="[required] App JSON"
                        )
    parser.add_argument("--dry-run", "-d",
                        help="Perform a dry run",
                        action="store_true"
                        )
    parser.add_argument("--force", "-f",
                        help="Perform deployment un-prompted",
                        action="store_true"
                        )
    parser.add_argument("--step-delay", "-s",
                        help="Delay between each successive deployment step",
                        type=int, default=5
                        )
    parser.add_argument("--initial-instances", "-i",
                        help="Initial number of app instances to launch",
                        type=int, default=1
                        )
    parser.add_argument("--resume", "-r",
                        help="Resume from a previous deployment",
                        action="store_true"
                        )
    parser = set_logging_args(parser)
    parser = set_marathon_auth_args(parser)
    return parser


if __name__ == '__main__':
    # Process arguments
    arg_parser = get_arg_parser()
    args = arg_parser.parse_args()

    # Print the long help text if flag is set
    if args.longhelp:
        print(__doc__)
        sys.exit()
    # otherwise make sure that a Marathon URL was specified
    else:
        if args.marathon is None:
            arg_parser.error('argument --marathon/-m is required')
        if args.marathon_lb is None:
            arg_parser.error('argument --marathon-lb/-l is required')
        if args.json is None:
            arg_parser.error('argument --json/-j is required')

    # Set request retries
    s = requests.Session()
    a = requests.adapters.HTTPAdapter(max_retries=3)
    s.mount('http://', a)

    # Setup logging
    setup_logging(logger, args.syslog_socket, args.log_format)

    process_json(args)
