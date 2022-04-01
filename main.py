# Copyright (c) 2021, eQualit.ie inc.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

from pyaml_env import parse_config

from config_generation.site_dict import get_all_sites

import argparse
import os
import click

from config_generation import (
    generate_bind_config,
    generate_nginx_config,
    generate_banjax_config,
    generate_edgemanage_config,
    generate_legacy_filebeat_config,
)
from config_generation.generate_elastic_keys import generate_new_elastic_certs

from orchestration.everything import (
        install_everything,
        install_controller,
        install_edges,
        gather_info,
        install_base,
)
from orchestration.run_container.base_class import find_existing_container
from orchestration.run_container.base_class import get_persisted_config

from orchestration.run_container.elasticsearch import Elasticsearch, attempt_to_authenticate
from orchestration.run_container.banjax import Banjax
from orchestration.hosts import docker_client_for_host, run_local_or_remote_noraise, host_to_role

import logging
from util.helpers import get_logger, get_config_yml_path, path_to_output
from util.fetch_site_yml import fetch_site_yml
from util.decrypt_and_verify_cert_bundles import main as decrypt_and_verify_cert_bundles

logger = get_logger(__name__)


def get_host_by_name(config, name):
    for host in [config['controller']] + config['edges']:
        if host['hostname'] == name:
            return host
        elif host['hostname'].split('.')[0] == name:
            return host


def comma_separated_names_to_hosts(config, names):
    names = names.split(",")
    return [get_host_by_name(config, n) for n in names]


def hosts_arg_to_hosts(config, hosts_arg):
    if hosts_arg == "all":
        return [config['controller']] + config['edges']
    elif hosts_arg == "controller":
        return [config['controller']]
    elif hosts_arg == "edges":
        return config['edges']
    else:
        return comma_separated_names_to_hosts(config, hosts_arg)


def gen_config(config, all_sites, timestamp):
    logger.info('>>> Generating bind config...')
    generate_bind_config(config, all_sites, timestamp)

    logger.info('>>> Generating nginx config...')
    generate_nginx_config(all_sites, config, timestamp)

    logger.info('>>> Generating banjax-next config...')
    generate_banjax_config(config, all_sites, timestamp)

    logger.info('>>> Generating edgemanage config...')
    generate_edgemanage_config(config, all_sites, timestamp)

    if config['logging']['mode'] == 'logstash_external':
        logger.info('>>> Generating legacy-filebeat config...')
        generate_legacy_filebeat_config(config, all_sites, timestamp)


def run_remote_commands(config, hosts, command):
    for host in hosts:
        if host_to_role(config, host) == "controller":
            continue  # controller doesn't have banjax

        proc = run_local_or_remote_noraise(config, host, command, logger)

        logger.info(f"===== {host['hostname']}")
        for line in proc.stdout.decode().splitlines():
            print(line)


def old_entry_point():
    # todo: many things to be fleshed out to deflect-next config
    config = parse_config(get_config_yml_path())

    argparser = argparse.ArgumentParser()

    argparser.add_argument(
        "--hosts", dest="hosts_arg", default="all",
        help="comma-separated hostnames OR 'controller' OR 'edges' OR 'all'"
    )
    argparser.add_argument(
        "-a", "--action", dest="action", required=True,
        choices=[
            "info",
            "install-base",
            "gen-config",
            "install-config",
            "install-controller",
            "install-edges",
            "test-es-auth",
            "install-es",
            "install-banjax",
            "kill-all-containers",
            "gen-new-elastic-certs",
            "get-nginx-errors",
            "show-useful-curl-commands",
            "get-banjax-decision-lists",
            "get-banjax-rate-limit-states",
            "get-nginx-and-banjax-config-versions",
            "check-cert-expiry",
            "fetch-site-yml",
            "decrypt_and_verify_cert_bundles"
        ],
        help="what to do to the hosts"
    )

    args = argparser.parse_args()

    hosts = hosts_arg_to_hosts(config, args.hosts_arg)

    if args.action == "info":
        gather_info(config, hosts)

    elif args.action == "install-base":
        install_base(config, hosts, logger)

    elif args.action == "gen-config":
        all_sites, timestamp = get_all_sites(config)
        gen_config(config, all_sites, timestamp)

    elif args.action == "install-config":
        all_sites, timestamp = get_all_sites(config)
        install_everything(config, all_sites, timestamp)

    elif args.action == "install-controller":
        all_sites, timestamp = get_all_sites(config)
        install_controller(config, all_sites, timestamp)

    elif args.action == "install-edges":
        all_sites, timestamp = get_all_sites(config)
        install_edges(config, all_sites, timestamp)

    elif args.action == "install-es":
        all_sites, timestamp = get_all_sites(config)
        client = docker_client_for_host(config['controller'], config=config)
        es = Elasticsearch(client, config, find_existing=True, logger=logger)
        es.update(timestamp)

    elif args.action == "install-banjax":
        all_sites, timestamp = get_all_sites(config)
        for host in hosts:
            client = docker_client_for_host(host, config=config)
            banjax = Banjax(client, config, kill_existing=True, logger=logger)
            banjax.update(timestamp)

    elif args.action == "test-es-auth":
        attempt_to_authenticate(config['controller']['ip'], logger)

    elif args.action == "kill-all-containers":
        command = "docker kill $(docker ps -q)"
        for host in hosts:
            proc = run_local_or_remote_noraise(config, host, command, logger)

    elif args.action == "gen-new-elastic-certs":
        generate_new_elastic_certs(config, logger)

    elif args.action == "get-nginx-errors":
        for host in hosts:
            client = docker_client_for_host(host, config=config)
            extra_label = "ngx_log_file=error-log"
            container = find_existing_container(client, "nginx-log-tailer", extra_label, config, logger)
            if not container:
                logger.info(f"===== nginx error log tailer not found on {host['hostname']}")
                continue

            logger.info(f"===== nginx error logs from {host['hostname']} =====")
            for line in container.logs().splitlines():
                logger.info(f"\t {line.decode()}")

    elif args.action == "show-useful-curl-commands":
        p_conf = get_persisted_config()
        elastic_password = p_conf.get('elastic_password', "<doesn't exist yet>")

        print("# test the ES certs + creds:\n"
              f"curl -v --resolve {config['controller']['hostname']}:9200:{config['controller']['ip']} --cacert persisted/elastic_certs/ca.crt https://{config['controller']['hostname']}:9200 --user 'elastic:{elastic_password}'")

        print("\n# test a site through a specific edge:")
        for edge in config['edges']:
            print(f"curl --resolve test-origin.{config['system_root_zone']}:443:{edge['ip']} --cacert persisted/pebble_ca.crt https://test-origin.{config['system_root_zone']}")
        for edge in config['edges']:
            print(f"curl -vI --resolve example.com:443:{edge['ip']} --cacert persisted/pebble_ca.crt https://example.com")

    # XXX duplication
    elif args.action == "get-banjax-decision-lists":
        logger.setLevel(logging.INFO)
        command = "curl --silent --header 'Host: banjax' 127.0.0.1/decision_lists"
        for host in hosts:
            if host_to_role(config, host) == "controller":
                continue  # controller doesn't have banjax

            proc = run_local_or_remote_noraise(config, host, command, logger)

            logger.info(f"===== decision lists on {host['hostname']}")
            for line in proc.stdout.decode().splitlines():
                print(line)

    # XXX duplication
    elif args.action == "get-banjax-rate-limit-states":
        logger.setLevel(logging.INFO)
        command = "curl --silent --header 'Host: banjax' 127.0.0.1/rate_limit_states"
        for host in hosts:
            if host_to_role(config, host) == "controller":
                continue  # controller doesn't have banjax

            proc = run_local_or_remote_noraise(config, host, command, logger)

            logger.info(f"===== rate limit states on {host['hostname']}")
            for line in proc.stdout.decode().splitlines():
                print(line)

    # XXX duplication
    elif args.action == "get-nginx-and-banjax-config-versions":
        logger.setLevel(logging.INFO)
        # XXX this should require a good host header like the banjax one does
        nginx_command = "curl --silent 127.0.0.1/info"
        banjax_command = "curl --silent --header 'Host: banjax' 127.0.0.1/info"
        nginx_proc, banjax_proc = None, None
        for host in hosts:
            nginx_proc = run_local_or_remote_noraise(config, host, nginx_command, logger)
            if host_to_role(config, host) == "edge":
                banjax_proc = run_local_or_remote_noraise(config, host, banjax_command, logger)

            logger.info(f"===== nginx and banjax versions on {host['hostname']}")
            for line in nginx_proc.stdout.decode().splitlines():
                print(f"\t nginx: {line}")
            if banjax_proc:
                for line in banjax_proc.stdout.decode().splitlines():
                    print(f"\t banjax: {line}")

    elif args.action == "check-cert-expiry":
        from cryptography import x509
        from cryptography.hazmat.backends import default_backend

        all_sites, timestamp = get_all_sites(config)
        # flatten...
        sites = {**all_sites['client'], **all_sites['system']}

        latest_cert_dir = os.path.join(path_to_output(), timestamp, "archive")
        for hostname, site in sites.items():
            site_dir = os.path.join(latest_cert_dir, hostname)
            if not os.path.isdir(site_dir):
                logger.info(f"site: {hostname} not found under output/archive")
                continue
            cert_bytes = None
            with open(os.path.join(site_dir, "cert1.pem"), "rb") as f:
                cert_bytes = f.read()
            cert = x509.load_pem_x509_certificate(cert_bytes, default_backend())
            logger.info(f"subject: {cert.subject}, issuer: {cert.issuer}, expires: {cert.not_valid_after}")

    elif args.action == "fetch-site-yml":
        fetch_site_yml(config['fetch_site_yml'], logger)

    elif args.action == "decrypt_and_verify_cert_bundles":
        all_sites, timestamp = get_all_sites(config)
        decrypt_and_verify_cert_bundles(all_sites, timestamp)


@click.group()
@click.pass_context
@click.option('--debug/--no-debug', default=False,
              help="This overrides global_config log level to DEBUG")
@click.option('--host', '-h', default='all',
              help='"all", "controller", "edges" or comma seperate hostname. '
                   'For example: "edge1,edge2,edge3" (subdomain name) '
                   'or full hostname "edge1.dev.deflect.network"')
def cli_base(ctx, debug, host):
    ctx.ensure_object(dict)
    ctx.obj['debug'] = debug
    ctx.obj['config'] = parse_config(get_config_yml_path())
    ctx.obj['host'] = host
    ctx.obj['_hosts'] = hosts_arg_to_hosts(ctx.obj['config'], host)
    click.echo(f"hosts: {ctx.obj['_hosts']}")


@click.command('info', help='Fetch docker version via SSH for testing')
@click.pass_context
def _gather_info(ctx):
    gather_info(ctx.obj['config'], ctx.obj['_hosts'])


@click.command('install-base', help='Install required package on target')
@click.pass_context
def _install_base(ctx):
    install_base(ctx.obj['config'], ctx.obj['_hosts'], logger)


@click.command('gen-config', help='Generate config from input dir')
@click.pass_context
def _gen_config(ctx):
    all_sites, timestamp = get_all_sites(ctx.obj['config'])
    gen_config(ctx.obj['config'], all_sites, timestamp)


@click.command('install-config', help='Install config to target')
@click.pass_context
def _install_config(ctx):
    all_sites, timestamp = get_all_sites(ctx.obj['config'])
    if ctx.obj['host'] == 'edges':
        install_edges(ctx.obj['config'], all_sites, timestamp)
    elif ctx.obj['host'] == 'controller':
        install_controller(ctx.obj['config'], all_sites, timestamp)
    else:
        install_everything(ctx.obj['config'], all_sites, timestamp)


@click.command('install-es', help='Install Elasticsearch')
@click.pass_context
def _install_es(ctx):
    all_sites, timestamp = get_all_sites(ctx.obj['config'])
    client = docker_client_for_host(ctx.obj['config']['controller'], config=ctx.obj['config'])
    es = Elasticsearch(client, ctx.obj['config'], find_existing=True, logger=logger)
    es.update(timestamp)



@click.command('install-banjax', help='Install and update banjax')
@click.pass_context
def _install_banjax(ctx):
    all_sites, timestamp = get_all_sites(ctx.obj['config'])
    for host in ctx.obj['_hosts']:
        client = docker_client_for_host(host, config=ctx.obj['config'])
        banjax = Banjax(client, ctx.obj['config'], kill_existing=True, logger=logger)
        banjax.update(timestamp)


@click.command('test-es-auth', help='Attempt to authenticate with saved ES auth')
@click.pass_context
def _test_es_auth(ctx):
    attempt_to_authenticate(ctx.obj['config']['controller']['ip'], logger)


@click.command('kill-all-containers', help='Run docker kill $(docker ps -q) on target')
@click.pass_context
def _kill_all_containers(ctx):
    command = "docker kill $(docker ps -q)"
    for host in ctx.obj['_hosts']:
        run_local_or_remote_noraise(ctx.obj['config'], host, command, logger)


@click.command('gen-new-elastic-certs', help='Generate new ES certs')
@click.pass_context
def _gen_new_elastic_certs(ctx):
    generate_new_elastic_certs(ctx.obj['config'], logger)


@click.command('get-nginx-errors', help='Get nginx errors')
@click.pass_context
def _get_nginx_errors(ctx):
    hosts = ctx.obj['_hosts']
    config = ctx.obj['config']
    for host in hosts:
        client = docker_client_for_host(host, config=config)
        extra_label = "ngx_log_file=error-log"
        container = find_existing_container(client, "nginx-log-tailer", extra_label, config, logger)
        if not container:
            logger.info(f"===== nginx error log tailer not found on {host['hostname']}")
            continue

        logger.info(f"===== nginx error logs from {host['hostname']} =====")
        for line in container.logs().splitlines():
            logger.info(f"\t {line.decode()}")


@click.command('show-useful-curl-commands', help='Print curl commands for ES and edge testing')
@click.option('--domain', '-d', default='example.com', help="Domain for testing")
@click.pass_context
def _show_useful_curl_commands(ctx, domain):
    hosts = ctx.obj['_hosts']
    config = ctx.obj['config']
    p_conf = get_persisted_config()
    elastic_password = p_conf.get('elastic_password', "<doesn't exist yet>")

    print("# test the ES certs + creds:\n"
          f"curl -v --resolve {config['controller']['hostname']}:9200:{config['controller']['ip']} --cacert persisted/elastic_certs/ca.crt https://{config['controller']['hostname']}:9200 --user 'elastic:{elastic_password}'")

    print("\n# test a site through a specific edge:")
    for edge in hosts:
        print(f"curl --resolve test-origin.{config['system_root_zone']}:443:{edge['ip']} --cacert persisted/pebble_ca.crt https://test-origin.{config['system_root_zone']}")
    for edge in hosts:
        print(f"curl --resolve example.com:443:{edge['ip']} --cacert persisted/pebble_ca.crt https://{domain}  # {edge['hostname']}")
    for edge in hosts:
        insecure = ' --insecure ' if config['server_env'] == 'staging' else ' '
        print(f"curl --resolve example.com:443:{edge['ip']}{insecure}https://{domain}  # {edge['hostname']}")


@click.command('get-banjax-decision-lists',
                help='Call banjax control endpoint')
@click.pass_context
def _get_banjax_decision_lists(ctx):
    command = "curl --silent --header 'Host: banjax' 127.0.0.1/decision_lists"
    run_remote_commands(ctx.obj['config'], ctx.obj['_hosts'], command)


@click.command('get-banjax-rate-limit-states',
               help='Call banjax control endpoint for rate limit states.')
@click.pass_context
def _get_banjax_rate_limit_states(ctx):
    command = "curl --silent --header 'Host: banjax' 127.0.0.1/rate_limit_states"
    run_remote_commands(ctx.obj['config'], ctx.obj['_hosts'], command)


@click.command('get-nginx-banjax-conf-versions',
               help='See the config version (from site dict) that nginx and banjax are running.')
@click.pass_context
def _get_nginx_and_banjax_config_versions(ctx):
    hosts = ctx.obj['_hosts']
    config = ctx.obj['config']
    nginx_command = "curl --silent 127.0.0.1/info"
    banjax_command = "curl --silent --header 'Host: banjax' 127.0.0.1/info"
    nginx_proc, banjax_proc = None, None
    for host in hosts:
        nginx_proc = run_local_or_remote_noraise(config, host, nginx_command, logger)
        if host_to_role(config, host) == "edge":
            banjax_proc = run_local_or_remote_noraise(config, host, banjax_command, logger)

        logger.info(f"===== nginx and banjax versions on {host['hostname']}")
        for line in nginx_proc.stdout.decode().splitlines():
            print(f"\t nginx: {line}")
        if banjax_proc:
            for line in banjax_proc.stdout.decode().splitlines():
                print(f"\t banjax: {line}")


@click.command('check-cert-expiry',
               help='Loop through all our certs and print the expiration time')
@click.pass_context
def _check_cert_expiry(ctx):
    from cryptography import x509
    from cryptography.hazmat.backends import default_backend

    all_sites, timestamp = get_all_sites(ctx.obj['config'])
    # flatten...
    sites = {**all_sites['client'], **all_sites['system']}

    latest_cert_dir = os.path.join(path_to_output(), timestamp, "archive")
    for hostname, site in sites.items():
        site_dir = os.path.join(latest_cert_dir, hostname)
        if not os.path.isdir(site_dir):
            logger.info(f"site: {hostname} not found under output/archive")
            continue
        cert_bytes = None
        with open(os.path.join(site_dir, "cert1.pem"), "rb") as f:
            cert_bytes = f.read()
        cert = x509.load_pem_x509_certificate(cert_bytes, default_backend())
        logger.info(f"subject: {cert.subject}, issuer: {cert.issuer}, expires: {cert.not_valid_after}")


@click.command('fetch-site-yml', help='Fetch site.yml file from dashboard')
@click.pass_context
def _fetch_site_yml(ctx):
    fetch_site_yml(ctx.obj['config']['fetch_site_yml'], logger)


@click.command('decrypt-verify-cert', help='Decrypt and verify cert bundles')
@click.pass_context
def _decrypt_and_verify_cert_bundles(ctx):
    all_sites, timestamp = get_all_sites(ctx.obj['config'])
    decrypt_and_verify_cert_bundles(all_sites, timestamp)


cli_base.add_command(_gather_info)
cli_base.add_command(_install_base)
cli_base.add_command(_gen_config)
cli_base.add_command(_install_config)
cli_base.add_command(_install_es)
cli_base.add_command(_install_banjax)
cli_base.add_command(_test_es_auth)
cli_base.add_command(_kill_all_containers)
cli_base.add_command(_gen_new_elastic_certs)
cli_base.add_command(_get_nginx_errors)
cli_base.add_command(_show_useful_curl_commands)
cli_base.add_command(_get_banjax_decision_lists)
cli_base.add_command(_get_banjax_rate_limit_states)
cli_base.add_command(_get_nginx_and_banjax_config_versions)
cli_base.add_command(_check_cert_expiry)
cli_base.add_command(_fetch_site_yml)
cli_base.add_command(_decrypt_and_verify_cert_bundles)


if __name__ == '__main__':
    cli_base()
