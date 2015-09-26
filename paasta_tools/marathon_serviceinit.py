#!/usr/bin/env python
import datetime
import logging
import sys

import humanize
import isodate
from mesos.cli.exceptions import SlaveDoesNotExist
import requests_cache

from paasta_tools import marathon_tools
from paasta_tools.mesos_tools import get_running_tasks_from_active_frameworks
from paasta_tools.mesos_tools import get_non_running_tasks_from_active_frameworks
from paasta_tools.mesos_tools import get_mesos_slaves_grouped_by_attribute
from paasta_tools.monitoring.replication_utils import match_backends_and_tasks, backend_is_up
from paasta_tools.smartstack_tools import DEFAULT_SYNAPSE_PORT
from paasta_tools.smartstack_tools import get_backends
from paasta_tools.utils import compose_job_id
from paasta_tools.utils import datetime_from_utc_to_local
from paasta_tools.utils import is_under_replicated
from paasta_tools.utils import _log
from paasta_tools.utils import NoDockerImageError
from paasta_tools.utils import PaastaColors
from paasta_tools.utils import remove_ansi_escape_sequences
from paasta_tools.utils import SPACER
from paasta_tools.utils import timeout
from paasta_tools.utils import TimeoutError

log = logging.getLogger('__main__')
log.addHandler(logging.StreamHandler(sys.stdout))

RUNNING_TASK_FORMAT = '    {0[0]:<37}{0[1]:<20}{0[2]:<10}{0[3]:<6}{0[4]:}'
NON_RUNNING_TASK_FORMAT = '    {0[0]:<37}{0[1]:<20}{0[2]:<33}{0[3]:}'


def start_marathon_job(service, instance, app_id, normal_instance_count, client, cluster):
    name = PaastaColors.cyan(compose_job_id(service, instance))
    _log(
        service_name=service,
        line="EmergencyStart: scaling %s up to %d instances" % (name, normal_instance_count),
        component='deploy',
        level='event',
        cluster=cluster,
        instance=instance
    )
    client.scale_app(app_id, instances=normal_instance_count, force=True)


def stop_marathon_job(service, instance, app_id, client, cluster):
    name = PaastaColors.cyan(compose_job_id(service, instance))
    _log(
        service_name=service,
        line="EmergencyStop: Scaling %s down to 0 instances" % (name),
        component='deploy',
        level='event',
        cluster=cluster,
        instance=instance
    )
    client.scale_app(app_id, instances=0, force=True)  # TODO do we want to capture the return val of any client calls?


def restart_marathon_job(service, instance, app_id, normal_instance_count, client, cluster):
    stop_marathon_job(service, instance, app_id, client, cluster)
    start_marathon_job(service, instance, app_id, normal_instance_count, client, cluster)


def get_bouncing_status(service, instance, client, job_config):
    apps = marathon_tools.get_matching_appids(service, instance, client)
    bounce_method = job_config.get_bounce_method()
    app_count = len(apps)
    if app_count == 0:
        return PaastaColors.red("Stopped")
    elif app_count == 1:
        return PaastaColors.green("Running")
    elif app_count > 1:
        return PaastaColors.yellow("Bouncing (%s)" % bounce_method)
    else:
        return PaastaColors.red("Unknown (count: %s)" % app_count)


def status_desired_state(service, instance, client, job_config):
    status = get_bouncing_status(service, instance, client, job_config)
    desired_state = job_config.get_desired_state_human()
    return "State:      %s - Desired state: %s" % (status, desired_state)


def status_marathon_job(service, instance, app_id, normal_instance_count, client):
    name = PaastaColors.cyan(compose_job_id(service, instance))
    if marathon_tools.is_app_id_running(app_id, client):
        app = client.get_app(app_id)
        running_instances = app.tasks_running
        if len(app.deployments) == 0:
            deploy_status = PaastaColors.bold("Running")
        else:
            deploy_status = PaastaColors.yellow("Deploying")
        if running_instances >= normal_instance_count:
            status = PaastaColors.green("Healthy")
            instance_count = PaastaColors.green("(%d/%d)" % (running_instances, normal_instance_count))
        elif running_instances == 0:
            status = PaastaColors.yellow("Critical")
            instance_count = PaastaColors.red("(%d/%d)" % (running_instances, normal_instance_count))
        else:
            status = PaastaColors.yellow("Warning")
            instance_count = PaastaColors.yellow("(%d/%d)" % (running_instances, normal_instance_count))
        return "Marathon:   %s - up with %s instances. Status: %s." % (status, instance_count, deploy_status)
    else:
        red_not = PaastaColors.red("NOT")
        status = PaastaColors.red("Critical")
        return "Marathon:   %s - %s (app %s) is %s running in Marathon." % (status, name, app_id, red_not)


def get_verbose_status_of_marathon_app(app):
    """Takes a given marathon app object and returns the verbose details
    about the tasks, times, hosts, etc"""
    output = []
    create_datetime = datetime_from_utc_to_local(isodate.parse_datetime(app.version))
    output.append("  Marathon app ID: %s" % PaastaColors.bold(app.id))
    output.append("    App created: %s (%s)" % (str(create_datetime), humanize.naturaltime(create_datetime)))
    output.append("    Tasks:  Mesos Task ID                  Host deployed to         Deployed at what localtime")
    for task in app.tasks:
        local_deployed_datetime = datetime_from_utc_to_local(task.staged_at)
        if task.host is not None:
            hostname = "%s:%s" % (task.host.split(".")[0], task.ports[0])
        else:
            hostname = "Unknown"
        format_tuple = (
            get_task_uuid(task.id),
            hostname,
            local_deployed_datetime.strftime("%Y-%m-%dT%H:%M"),
            humanize.naturaltime(local_deployed_datetime),
        )
        output.append('      {0[0]:<37}{0[1]:<25} {0[2]:<17}({0[3]:})'.format(format_tuple))
    if len(app.tasks) == 0:
        output.append("      No tasks associated with this marathon app")
    return app.tasks, "\n".join(output)


def status_marathon_job_verbose(service, instance, client):
    """Returns detailed information about a marathon apps for a service
    and instance. Does not make assumptions about what the *exact*
    appid is, but instead does a fuzzy match on any marathon apps
    that match the given service.instance"""
    all_tasks = []
    all_output = []
    # For verbose mode, we want to see *any* matching app. As it may
    # not be the one that we think should be deployed. For example
    # during a bounce we want to see the old and new ones.
    for appid in marathon_tools.get_matching_appids(service, instance, client):
        app = client.get_app(appid)
        tasks, output = get_verbose_status_of_marathon_app(app)
        all_tasks.extend(tasks)
        all_output.append(output)
    return all_tasks, "\n".join(all_output)


def haproxy_backend_report(normal_instance_count, up_backends):
    """Given that a service is in smartstack, this returns a human readable
    report of the up backends"""
    # TODO: Take into account a configurable threshold, PAASTA-1102
    crit_threshold = 50
    under_replicated, ratio = is_under_replicated(num_available=up_backends,
                                                  expected_count=normal_instance_count,
                                                  crit_threshold=crit_threshold)
    if under_replicated:
        status = PaastaColors.red("Critical")
        count = PaastaColors.red("(%d/%d, %d%%)" % (up_backends, normal_instance_count, ratio))
    else:
        status = PaastaColors.green("Healthy")
        count = PaastaColors.green("(%d/%d)" % (up_backends, normal_instance_count))
    up_string = PaastaColors.bold('UP')
    return "%s - in haproxy with %s total backends %s in this namespace." % (status, count, up_string)


def pretty_print_haproxy_backend(backend, is_correct_instance):
    """Pretty Prints the status of a given haproxy backend
    Takes the fields described in the CSV format of haproxy:
    http://www.haproxy.org/download/1.5/doc/configuration.txt
    And tries to make a good guess about how to represent them in text
    """
    backend_name = backend['svname']
    backend_hostname = backend_name.split("_")[-1]
    backend_port = backend_name.split("_")[0].split(":")[-1]
    pretty_backend_name = "%s:%s" % (backend_hostname, backend_port)
    if backend['status'] == "UP":
        status = PaastaColors.default(backend['status'])
    elif backend['status'] == 'DOWN' or backend['status'] == 'MAINT':
        status = PaastaColors.red(backend['status'])
    else:
        status = PaastaColors.yellow(backend['status'])
    lastcheck = "%s/%s in %sms" % (backend['check_status'], backend['check_code'], backend['check_duration'])
    lastchange = humanize.naturaltime(datetime.timedelta(seconds=int(backend['lastchg'])))

    status_text = '      {name:<32}{lastcheck:<20}{lastchange:<16}{status:}'.format(
        name=pretty_backend_name,
        lastcheck=lastcheck,
        lastchange=lastchange,
        status=status,
    )

    if is_correct_instance:
        return PaastaColors.color_text(PaastaColors.DEFAULT, status_text)
    else:
        return PaastaColors.color_text(PaastaColors.GREY, remove_ansi_escape_sequences(status_text))


def status_smartstack_backends(service, instance, job_config, cluster, tasks, expected_count, soa_dir, verbose):
    """Returns detailed information about smartstack backends for a service
    and instance.
    return: A newline separated string of the smarststack backend status
    """
    output = []
    nerve_ns = marathon_tools.read_namespace_for_service_instance(service, instance, cluster)
    service_instance = compose_job_id(service, nerve_ns)

    if instance != nerve_ns:
        ns_string = PaastaColors.bold(nerve_ns)
        output.append("Smartstack: N/A - %s is announced in the %s namespace." % (instance, ns_string))
        # If verbose mode is specified, then continue to show backends anyway, otherwise stop early
        if not verbose:
            return "\n".join(output)

    service_namespace_config = marathon_tools.load_service_namespace_config(service, instance, soa_dir=soa_dir)
    discover_location_type = service_namespace_config.get_discover()
    monitoring_blacklist = job_config.get_monitoring_blacklist()
    unique_attributes = get_mesos_slaves_grouped_by_attribute(
        attribute=discover_location_type, blacklist=monitoring_blacklist)
    if len(unique_attributes) == 0:
        output.append("Smartstack: ERROR - %s is NOT in smartstack at all!" % service_instance)
    else:
        output.append("Smartstack:")
        if verbose:
            output.append("  Haproxy Service Name: %s" % service_instance)
            output.append("  Backends: Name                      LastCheck           LastChange      Status")

        output.extend(pretty_print_smartstack_backends_for_locations(
            service_instance,
            tasks,
            unique_attributes,
            expected_count,
            verbose
        ))
    return "\n".join(output)


def pretty_print_smartstack_backends_for_locations(service_instance, tasks, locations, expected_count, verbose):
    """
    Pretty prints the status of smartstack backends of a specified service and instance in the specified locations
    """
    output = []
    expected_count_per_location = int(expected_count / len(locations))
    for location in sorted(locations):
        hosts = locations[location]
        # arbitrarily choose the first host with a given attribute to query for replication stats
        synapse_host = hosts[0]
        sorted_backends = sorted(get_backends(service_instance,
                                              synapse_host=synapse_host,
                                              synapse_port=DEFAULT_SYNAPSE_PORT),
                                 key=lambda backend: backend['status'],
                                 reverse=True)  # Specify reverse so that backends in 'UP' are placed above 'MAINT'
        matched_tasks = match_backends_and_tasks(sorted_backends, tasks)
        running_count = sum(1 for backend, task in matched_tasks if backend and backend_is_up(backend))
        output.append("    %s - %s" %
                      (location, haproxy_backend_report(expected_count_per_location, running_count)))

        # If verbose mode is specified, show status of individual backends
        if verbose:
            for backend, task in matched_tasks:
                if backend is not None:
                    output.append(pretty_print_haproxy_backend(backend, task is not None))
    return output


@timeout()
def get_cpu_usage(task):
    """Calculates a metric of used_cpu/allocated_cpu
    To do this, we take the total number of cpu-seconds the task has consumed,
    (the sum of system and user time), OVER the total cpu time the task
    has been allocated.

    The total time a task has been allocated is the total time the task has
    been running (https://github.com/mesosphere/mesos/blob/0b092b1b0/src/webui/master/static/js/controllers.js#L140)
    multiplied by the "shares" a task has.
    """
    try:
        start_time = round(task['statuses'][0]['timestamp'])
        current_time = int(datetime.datetime.now().strftime('%s'))
        duration_seconds = current_time - start_time
        # The CPU shares has an additional .1 allocated to it for executor overhead.
        # We subtract this to the true number
        # (https://github.com/apache/mesos/blob/dc7c4b6d0bcf778cc0cad57bb108564be734143a/src/slave/constants.hpp#L100)
        cpu_shares = task.cpu_limit - .1
        allocated_seconds = duration_seconds * cpu_shares
        used_seconds = task.stats.get('cpus_system_time_secs', 0.0) + task.stats.get('cpus_user_time_secs', 0.0)
        if allocated_seconds == 0:
            return "Undef"
        percent = round(100 * (used_seconds / allocated_seconds), 1)
        percent_string = "%s%%" % percent
        if percent > 90:
            return PaastaColors.red(percent_string)
        else:
            return percent_string
    except (AttributeError, SlaveDoesNotExist):
        return "None"
    except TimeoutError:
        return "Timed Out"


@timeout()
def get_mem_usage(task):
    try:
        task_mem_limit = task.mem_limit
        task_rss = task.rss
        if task_mem_limit == 0:
            return "Undef"
        mem_percent = task_rss / task_mem_limit * 100
        mem_string = "%d/%dMB" % ((task_rss / 1024 / 1024), (task_mem_limit / 1024 / 1024))
        if mem_percent > 90:
            return PaastaColors.red(mem_string)
        else:
            return mem_string
    except (AttributeError, SlaveDoesNotExist):
        return "None"
    except TimeoutError:
        return "Timed Out"


def get_task_uuid(taskid):
    """Return just the UUID part of a mesos task id"""
    return taskid.split(SPACER)[-1]


def get_short_hostname_from_task(task):
    try:
        slave_hostname = task.slave['hostname']
        return slave_hostname.split(".")[0]
    except (AttributeError, SlaveDoesNotExist):
        return 'Unknown'


def get_first_status_timestamp(task):
    """Gets the first status timestamp from a task id and returns a human
    readable string with the local time and a humanized duration:
    ``2015-01-30 08:45:19.108820 (an hour ago)``
    """
    try:
        start_time_string = task['statuses'][0]['timestamp']
        start_time = datetime.datetime.fromtimestamp(float(start_time_string))
        return "%s (%s)" % (start_time.strftime("%Y-%m-%dT%H:%M"), humanize.naturaltime(start_time))
    except (IndexError, SlaveDoesNotExist):
        return "Unknown"


def pretty_format_running_mesos_task(task):
    """Returns a pretty formatted string of a running mesos task attributes"""
    format_tuple = (
        get_task_uuid(task['id']),
        get_short_hostname_from_task(task),
        get_mem_usage(task),
        get_cpu_usage(task),
        get_first_status_timestamp(task),
    )
    return RUNNING_TASK_FORMAT.format(format_tuple)


def pretty_format_non_running_mesos_task(task):
    """Returns a pretty formatted string of a running mesos task attributes"""
    format_tuple = (
        get_task_uuid(task['id']),
        get_short_hostname_from_task(task),
        get_first_status_timestamp(task),
        task['state'],
    )
    return PaastaColors.grey(NON_RUNNING_TASK_FORMAT.format(format_tuple))


def status_mesos_tasks(service, instance, normal_instance_count):
    job_id = marathon_tools.format_job_id(service, instance)
    running_and_active_tasks = get_running_tasks_from_active_frameworks(job_id)
    count = len(running_and_active_tasks)
    if count >= normal_instance_count:
        status = PaastaColors.green("Healthy")
        count = PaastaColors.green("(%d/%d)" % (count, normal_instance_count))
    elif count == 0:
        status = PaastaColors.red("Critical")
        count = PaastaColors.red("(%d/%d)" % (count, normal_instance_count))
    else:
        status = PaastaColors.yellow("Warning")
        count = PaastaColors.yellow("(%d/%d)" % (count, normal_instance_count))
    running_string = PaastaColors.bold('TASK_RUNNING')
    return "Mesos:      %s - %s tasks in the %s state." % (status, count, running_string)


def status_mesos_tasks_verbose(service, instance):
    """Returns detailed information about the mesos tasks for a service"""
    output = []

    job_id = marathon_tools.format_job_id(service, instance)
    running_and_active_tasks = get_running_tasks_from_active_frameworks(job_id)
    output.append(RUNNING_TASK_FORMAT.format((
        "  Running Tasks:  Mesos Task ID",
        "Host deployed to",
        "Ram",
        "CPU",
        "Deployed at what localtime"
    )))
    for task in running_and_active_tasks:
        output.append(pretty_format_running_mesos_task(task))

    job_id = marathon_tools.format_job_id(service, instance)
    non_running_tasks = list(reversed(get_non_running_tasks_from_active_frameworks(job_id)[-10:]))
    output.append(PaastaColors.grey(NON_RUNNING_TASK_FORMAT.format((
        "  Non-Running Tasks:  Mesos Task ID",
        "Host deployed to",
        "Deployed at what localtime",
        "Status"
    ))))
    for task in non_running_tasks:
        output.append(pretty_format_non_running_mesos_task(task))

    return "\n".join(output)


def perform_command(command, service, instance, cluster, verbose, soa_dir):
    """Performs a start/stop/restart/status on an instance
    :param command: String of start, stop, restart, or status
    :param service: service name
    :param instance: instance name, like "main" or "canary"
    :param cluster: cluster name
    :param verbose: bool if the output should be verbose or not
    :returns: A unix-style return code
    """
    marathon_config = marathon_tools.load_marathon_config()
    job_config = marathon_tools.load_marathon_service_config(service, instance, cluster)
    try:
        app_id = marathon_tools.create_complete_config(service, instance, marathon_config, soa_dir=soa_dir)['id']
    except NoDockerImageError:
        job_name = compose_job_id(service, instance)
        print "Docker image for %s not in deployments.json. Exiting. Has Jenkins deployed it?" % job_name
        return 1

    normal_instance_count = job_config.get_instances()
    normal_smartstack_count = marathon_tools.get_expected_instance_count_for_namespace(service, instance)
    proxy_port = marathon_tools.get_proxy_port_for_instance(service, instance)

    client = marathon_tools.get_marathon_client(marathon_config.get_url(), marathon_config.get_username(),
                                                marathon_config.get_password())
    if command == 'start':
        start_marathon_job(service, instance, app_id, normal_instance_count, client, cluster)
    elif command == 'stop':
        stop_marathon_job(service, instance, app_id, client, cluster)
    elif command == 'restart':
        restart_marathon_job(service, instance, app_id, normal_instance_count, client, cluster)
    elif command == 'status':
        # Setting up transparent cache for http API calls
        requests_cache.install_cache('paasta_serviceinit', backend='memory')

        print status_desired_state(service, instance, client, job_config)
        print status_marathon_job(service, instance, app_id, normal_instance_count, client)
        tasks, out = status_marathon_job_verbose(service, instance, client)
        if verbose:
            print out
        print status_mesos_tasks(service, instance, normal_instance_count)
        if verbose:
            print status_mesos_tasks_verbose(service, instance)
        if proxy_port is not None:
            print status_smartstack_backends(
                service=service,
                instance=instance,
                cluster=cluster,
                job_config=job_config,
                tasks=tasks,
                expected_count=normal_smartstack_count,
                soa_dir=soa_dir,
                verbose=verbose,
            )
    else:
        # The command parser shouldn't have let us get this far...
        raise NotImplementedError("Command %s is not implemented!" % command)
    return 0


# vim: tabstop=4 expandtab shiftwidth=4 softtabstop=4
