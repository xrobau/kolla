#!/usr/bin/env python

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# TODO(jpeeler): Add clean up handler for SIGINT

import argparse
import datetime
import errno
import json
import logging
import os
import platform
import re
import requests
import shutil
import signal
import sys
import tarfile
import tempfile
from threading import Thread
import time

import docker
import git
import jinja2
from requests.exceptions import ConnectionError
import six

logging.basicConfig()
LOG = logging.getLogger(__name__)
LOG.setLevel(logging.INFO)

signal.signal(signal.SIGINT, signal.SIG_DFL)


class KollaDirNotFoundException(Exception):
    pass


class KollaUnknownBuildTypeException(Exception):
    pass


def docker_client():
    docker_kwargs = docker.utils.kwargs_from_env()
    return docker.Client(version='auto', **docker_kwargs)


class WorkerThread(Thread):

    def __init__(self, queue, config):
        self.queue = queue
        self.nocache = config['no_cache']
        self.forcerm = not config['keep']
        self.retries = config['retries']
        self.dc = docker_client()
        super(WorkerThread, self).__init__()

    def end_task(self, image):
        """Properly inform the queue we are finished"""
        # No matter whether the parent failed or not, we still process
        # the children. We have the code in place to catch a parent in
        # an 'error' status
        for child in image['children']:
            self.queue.put(child)
            LOG.debug('{}:Added image to queue'.format(child['name']))
        self.queue.task_done()
        LOG.debug('{}:Processed'.format(image['name']))

    def run(self):
        """Executes tasks until the queue is empty"""
        while True:
            try:
                image = self.queue.get()
                for _ in range(self.retries + 1):
                    self.builder(image)
                    if image['status'] in ['built', 'unmatched',
                                           'parent_error']:
                        break
            except ConnectionError as e:
                LOG.error(e)
                LOG.error('Make sure Docker is running and that you have '
                          'the correct privileges to run Docker (root)')
                image['status'] = "connection_error"
                break
            self.end_task(image)

    def process_source(self, image, source):
        dest_archive = os.path.join(image['path'], source['name'] + '-archive')

        if source.get('type') == 'url':
            LOG.debug("{}:Getting archive from {}".format(image['name'],
                                                          source['source']))
            r = requests.get(source['source'])

            if r.status_code == 200:
                with open(dest_archive, 'wb') as f:
                    f.write(r.content)
            else:
                LOG.error(
                    '{}:Failed to download archive: status_code {}'.format(
                        image['name'], r.status_code))
                image['status'] = "error"
                return

        elif source.get('type') == 'git':
            clone_dir = '{}-{}'.format(dest_archive,
                                       source['reference'].replace('/', '-'))
            try:
                LOG.debug("{}:Cloning from {}".format(image['name'],
                                                      source['source']))
                git.Git().clone(source['source'], clone_dir)
                LOG.debug("{}:Git checkout by reference {}".format(
                          image['name'], source['reference']))
                git.Git(clone_dir).checkout(source['reference'])
            except Exception as e:
                LOG.error("{}:Failed to get source from git".format(
                          image['name']))
                LOG.error("{}:Error:{}".format(image['name'], str(e)))
                # clean-up clone folder to retry
                shutil.rmtree(clone_dir)
                image['status'] = "error"
                return

            with tarfile.open(dest_archive, 'w') as tar:
                tar.add(clone_dir, arcname=os.path.basename(clone_dir))

        else:
            LOG.error("{}:Wrong source type '{}'".format(image['name'],
                                                         source.get('type')))
            image['status'] = "error"
            return

        # Set time on destination archive to epoch 0
        os.utime(dest_archive, (0, 0))

        return dest_archive

    def builder(self, image):
        LOG.debug('{}:Processing'.format(image['name']))
        if image['status'] == 'unmatched':
            return

        if (image['parent'] is not None and
                image['parent']['status'] in ['error', 'parent_error',
                                              'connection_error']):
            LOG.error('{}:Parent image error\'d with message "{}"'.format(
                      image['name'], image['parent']['status']))
            image['status'] = "parent_error"
            return

        image['status'] = "building"
        LOG.info('{}:Building'.format(image['name']))

        if 'source' in image and 'source' in image['source']:
            self.process_source(image, image['source'])
            if image['status'] == "error":
                return

        plugin_archives = list()
        plugins_path = os.path.join(image['path'], 'plugins')
        for plugin in image['plugins']:
            archive_path = self.process_source(image, plugin)
            if image['status'] == "error":
                return
            plugin_archives.append(archive_path)
        if plugin_archives:
            for plugin_archive in plugin_archives:
                with tarfile.open(plugin_archive, 'r') as plugin_archive_tar:
                    plugin_archive_tar.extractall(path=plugins_path)
        else:
            try:
                os.mkdir(plugins_path)
            except OSError as e:
                if e.errno == errno.EEXIST:
                    LOG.info('Directory {} already exist. Skipping.'.format(
                        plugins_path))
                else:
                    LOG.error('Failed to create directory {}: {}'.format(
                        plugins_path, e))
                    image['status'] = "error"
                    return
        with tarfile.open(os.path.join(image['path'], 'plugins-archive'),
                          'w') as tar:
            tar.add(plugins_path, arcname='plugins')

        # Pull the latest image for the base distro only
        pull = True if image['parent'] is None else False

        image['logs'] = str()
        for response in self.dc.build(path=image['path'],
                                      tag=image['fullname'],
                                      nocache=self.nocache,
                                      rm=True,
                                      pull=pull,
                                      forcerm=self.forcerm):
            stream = json.loads(response.decode('utf-8'))

            if 'stream' in stream:
                image['logs'] = image['logs'] + stream['stream']
                for line in stream['stream'].split('\n'):
                    if line:
                        LOG.info('{}:{}'.format(image['name'], line))

            if 'errorDetail' in stream:
                image['status'] = "error"
                LOG.error('{}:Error\'d with the following message'.format(
                          image['name']))
                for line in stream['errorDetail']['message'].split('\n'):
                    if line:
                        LOG.error('{}:{}'.format(image['name'], line))
                return

        image['status'] = "built"

        LOG.info('{}:Built'.format(image['name']))


def get_kolla_version():
    local_conf_path = os.path.join(find_base_dir(), "setup.cfg")
    config = six.moves.configparser.RawConfigParser()
    config.read(local_conf_path)
    version = config.get("metadata", "version")
    return version


def find_os_type():
    return platform.linux_distribution()


def find_base_dir():
    script_path = os.path.dirname(os.path.realpath(sys.argv[0]))
    if os.path.basename(script_path) == 'cmd':
        return os.path.realpath(os.path.join(script_path, '..', '..'))
    if os.path.basename(script_path) == 'bin':
        if find_os_type()[0] in ['Ubuntu', 'debian']:
            return '/usr/local/share/kolla'
        else:
            return '/usr/share/kolla'
    if os.path.exists(os.path.join(script_path, 'tests')):
        return script_path
    raise KollaDirNotFoundException(
        'I do not know where your Kolla directory is'
    )


def find_config_file(filename):
    global_conf_path = os.path.join('/etc/kolla', filename)
    local_conf_path = os.path.join(find_base_dir(), 'etc', 'kolla', filename)

    if os.access(global_conf_path, os.R_OK):
        return global_conf_path
    elif os.access(local_conf_path, os.R_OK):
        return local_conf_path
    else:
        raise KollaDirNotFoundException(
            'Cant find kolla config. Searched at: %s and %s' %
            (global_conf_path, local_conf_path)
        )


def merge_args_and_config(settings_from_config_file):
    parser = argparse.ArgumentParser(description='Kolla build script')

    defaults = {
        "base": "centos",
        "base_tag": "latest",
        "install_type": "binary",
        "keep": False,
        "maintainer": "Kolla Project (https://launchpad.net/kolla)",
        "namespace": "kollaglue",
        "no_cache": False,
        "push": False,
        "registry": None,
        "retries": 3,
        "tag": get_kolla_version(),
        "threads": 8
    }
    defaults.update(settings_from_config_file.items('kolla-build'))
    parser.set_defaults(**defaults)

    parser.add_argument('-b', '--base',
                        help='The base distro to use when building',
                        type=str)
    parser.add_argument('--base-tag',
                        help='The base distro image tag',
                        type=str)
    parser.add_argument('-d', '--debug',
                        help='Turn on debugging log level',
                        action='store_true')
    parser.add_argument('-i', '--include-header',
                        help=('Path to custom file to be added at '
                              'beginning of base Dockerfile'),
                        type=str)
    parser.add_argument('-I', '--include-footer',
                        help=('Path to custom file to be added at '
                              'end of Dockerfiles for final images'),
                        type=str)
    parser.add_argument('--keep',
                        help='Keep failed intermediate containers',
                        action='store_true')
    parser.add_argument('-n', '--namespace',
                        help='Set the Docker namespace name',
                        type=str)
    parser.add_argument('--no-cache',
                        help='Do not use the Docker cache when building',
                        action='store_true')
    parser.add_argument('-p', '--profile',
                        help=('Build a pre-defined set of images, see '
                              '[profiles] section in '
                              '{}'.format(
                                  find_config_file('kolla-build.conf'))),
                        type=str,
                        action='append')
    parser.add_argument('--push',
                        help='Push images after building',
                        action='store_true')
    parser.add_argument('-r', '--retries',
                        help='The number of times to retry while building',
                        type=int)
    parser.add_argument('regex',
                        help=('Build only images matching '
                              'regex and its dependencies'),
                        nargs='*')
    parser.add_argument('--registry',
                        help=("the docker registry host"),
                        type=str)
    parser.add_argument('-t', '--type',
                        help='The method of the Openstack install: binary,'
                             ' source, rdo, or rhos',
                        type=str,
                        dest='install_type')
    parser.add_argument('-T', '--threads',
                        help='The number of threads to use while building.'
                             ' (Note: setting to one will allow real time'
                             ' logging.)',
                        type=int)
    parser.add_argument('--tag',
                        help='Set the Docker tag',
                        type=str)
    parser.add_argument('--template',
                        help='DEPRECATED: All Dockerfiles are templates',
                        action='store_true',
                        default=True)
    parser.add_argument('--template-only',
                        help=("Don't build images. Generate Dockerfile only"),
                        action='store_true')
    return vars(parser.parse_args())


class KollaWorker(object):

    def __init__(self, config):
        self.base_dir = os.path.abspath(find_base_dir())
        LOG.debug("Kolla base directory: " + self.base_dir)
        self.images_dir = os.path.join(self.base_dir, 'docker')
        self.registry = config['registry']
        if self.registry:
            self.namespace = self.registry + '/' + config['namespace']
        else:
            self.namespace = config['namespace']
        self.base = config['base']
        self.base_tag = config['base_tag']
        self.install_type = config['install_type']
        self.tag = config['tag']
        self.images = list()

        if self.install_type == 'binary':
            self.install_metatype = 'rdo'
        elif self.install_type == 'source':
            self.install_metatype = 'mixed'
        elif self.install_type == 'rdo':
            self.install_type = 'binary'
            self.install_metatype = 'rdo'
        elif self.install_type == 'rhos':
            self.install_type = 'binary'
            self.install_metatype = 'rhos'
        else:
            raise KollaUnknownBuildTypeException(
                'Unknown install type'
            )

        self.image_prefix = self.base + '-' + self.install_type + '-'

        self.tag = config['tag']
        self.include_header = config['include_header']
        self.include_footer = config['include_footer']
        self.regex = config['regex']
        self.profile = config['profile']
        self.source_location = six.moves.configparser.SafeConfigParser()
        self.source_location.read(find_config_file('kolla-build.conf'))
        self.image_statuses_bad = dict()
        self.image_statuses_good = dict()
        self.image_statuses_unmatched = dict()
        self.maintainer = config['maintainer']

    def setup_working_dir(self):
        """Creates a working directory for use while building"""
        ts = time.time()
        ts = datetime.datetime.fromtimestamp(ts).strftime('%Y-%m-%d_%H-%M-%S_')
        self.temp_dir = tempfile.mkdtemp(prefix='kolla-' + ts)
        self.working_dir = os.path.join(self.temp_dir, 'docker')
        shutil.copytree(self.images_dir, self.working_dir)
        LOG.debug('Created working dir: {}'.format(self.working_dir))

    def set_time(self):
        for root, dirs, files in os.walk(self.working_dir):
            for file_ in files:
                os.utime(os.path.join(root, file_), (0, 0))
            for dir_ in dirs:
                os.utime(os.path.join(root, dir_), (0, 0))
        LOG.debug('Set atime and mtime to 0 for all content in working dir')

    def create_dockerfiles(self):
        for path in self.docker_build_paths:
            template_name = "Dockerfile.j2"
            env = jinja2.Environment(loader=jinja2.FileSystemLoader(path))
            template = env.get_template(template_name)
            values = {'base_distro': self.base,
                      'base_distro_tag': self.base_tag,
                      'install_metatype': self.install_metatype,
                      'image_prefix': self.image_prefix,
                      'install_type': self.install_type,
                      'namespace': self.namespace,
                      'tag': self.tag,
                      'maintainer': self.maintainer}
            if self.include_header:
                with open(self.include_header, 'r') as f:
                    values['include_header'] = f.read()
            if self.include_footer:
                with open(self.include_footer, 'r') as f:
                    values['include_footer'] = f.read()
            content = template.render(values)
            with open(os.path.join(path, 'Dockerfile'), 'w') as f:
                f.write(content)

    def find_dockerfiles(self):
        """Recursive search for Dockerfiles in the working directory"""
        self.docker_build_paths = list()
        path = self.working_dir
        filename = 'Dockerfile.j2'

        for root, dirs, names in os.walk(path):
            if filename in names:
                self.docker_build_paths.append(root)
                LOG.debug('Found {}'.format(root.split(self.working_dir)[1]))

        LOG.debug('Found {} Dockerfiles'.format(len(self.docker_build_paths)))

    def cleanup(self):
        """Remove temp files"""
        shutil.rmtree(self.temp_dir)

    def filter_images(self):
        """Filter which images to build"""
        filter_ = list()

        if self.regex:
            filter_ += self.regex

        if self.profile:
            for profile in self.profile:
                try:
                    filter_ += self.source_location.get('profiles',
                                                        profile
                                                        ).split(',')
                except six.moves.configparser.NoSectionError:
                    LOG.error('No [profiles] section found in {}'.format(
                        find_config_file('kolla-build.conf')))
                except six.moves.configparser.NoOptionError:
                    LOG.error('No profile named "{}" found in {}'.format(
                        self.profile,
                        find_config_file('kolla-build.conf')))

        if filter_:
            patterns = re.compile(r"|".join(filter_).join('()'))
            for image in self.images:
                if image['status'] == 'matched':
                    continue
                if re.search(patterns, image['name']):
                    image['status'] = 'matched'
                    while (image['parent'] is not None and
                           image['parent']['status'] != 'matched'):
                        image = image['parent']
                        image['status'] = 'matched'
                        LOG.debug('{}:Matched regex'.format(image['name']))
                else:
                    image['status'] = 'unmatched'
        else:
            for image in self.images:
                image['status'] = 'matched'

    def summary(self):
        """Walk the dictionary of images statuses and print results"""
        # For debug we print the logs again if the image error'd. This is to
        # to help us debug and it will be extra helpful in the gate.
        for image in self.images:
            if image['status'] == 'error':
                LOG.debug("{}:Failed with the following logs".format(
                          image['name']))
                for line in image['logs'].split('\n'):
                    if line:
                        LOG.debug("{}:{}".format(image['name'], ''.join(line)))

        self.get_image_statuses()

        if self.image_statuses_good:
            LOG.info("Successfully built images")
            LOG.info("=========================")
            for name in self.image_statuses_good.keys():
                LOG.info(name)

        if self.image_statuses_bad:
            LOG.info("Images that failed to build")
            LOG.info("===========================")
            for name, status in six.iteritems(self.image_statuses_bad):
                LOG.error('{}\r\t\t\t Failed with status: {}'.format(
                    name, status))

        if self.image_statuses_unmatched:
            LOG.debug("Images not matched for build by regex")
            LOG.debug("=====================================")
            for name in self.image_statuses_unmatched.keys():
                LOG.debug(name)

    def get_image_statuses(self):
        if any([self.image_statuses_bad,
                self.image_statuses_good,
                self.image_statuses_unmatched]):
            return (self.image_statuses_bad,
                    self.image_statuses_good,
                    self.image_statuses_unmatched)
        for image in self.images:
            if image['status'] == "built":
                self.image_statuses_good[image['name']] = image['status']
            elif image['status'] == "unmatched":
                self.image_statuses_unmatched[image['name']] = image['status']
            else:
                self.image_statuses_bad[image['name']] = image['status']
        return (self.image_statuses_bad,
                self.image_statuses_good,
                self.image_statuses_unmatched)

    def build_image_list(self):
        def process_source_installation(image, section):
            installation = dict()
            try:
                installation['type'] = \
                    self.source_location.get(section, 'type')
                installation['source'] = \
                    self.source_location.get(section, 'location')
                installation['name'] = section
                if installation['type'] == 'git':
                    installation['reference'] = \
                        self.source_location.get(section, 'reference')
            except six.moves.configparser.NoSectionError:
                LOG.debug('{}:No source location found'.format(section))
            return installation

        for path in self.docker_build_paths:
            # Reading parent image name
            with open(os.path.join(path, 'Dockerfile')) as f:
                content = f.read()

            image = dict()
            image['status'] = "unprocessed"
            image['name'] = os.path.basename(path)
            image['fullname'] = self.namespace + '/' + self.image_prefix + \
                image['name'] + ':' + self.tag
            image['path'] = path
            image['parent_name'] = content.split(' ')[1].split('\n')[0]
            if not image['parent_name'].startswith(self.namespace + '/'):
                image['parent'] = None
            image['children'] = list()
            image['plugins'] = list()

            if self.install_type == 'source':
                image['source'] = \
                    process_source_installation(image, image['name'])
                for plugin in [match.group(0) for match in
                               (re.search('{}-plugin-.+'.format(image['name']),
                                          section) for section in
                               self.source_location.sections()) if match]:
                    image['plugins'].append(
                        process_source_installation(image, plugin))

            self.images.append(image)

    def find_parents(self):
        """Associate all images with parents and children"""
        sort_images = dict()

        for image in self.images:
            sort_images[image['fullname']] = image

        for parent_name, parent in six.iteritems(sort_images):
            for image in sort_images.values():
                if image['parent_name'] == parent_name:
                    parent['children'].append(image)
                    image['parent'] = parent

    def build_queue(self):
        """Organizes Queue list

        Return a list of Queues that have been organized into a hierarchy
        based on dependencies
        """
        self.build_image_list()
        self.find_parents()
        self.filter_images()

        queue = six.moves.queue.Queue()

        for image in self.images:
            if image['parent'] is None:
                queue.put(image)
                LOG.debug('{}:Added image to queue'.format(image['name']))

        return queue


def push_image(image):
    dc = docker_client()
    image['push_logs'] = str()

    for response in dc.push(image['fullname'],
                            stream=True,
                            insecure_registry=True):
        stream = json.loads(response)

        if 'stream' in stream:
            image['push_logs'] = image['logs'] + stream['stream']
            LOG.info('{}'.format(stream['stream']))
        elif 'errorDetail' in stream:
            image['status'] = "error"
            LOG.error(stream['errorDetail']['message'])


def main():
    build_config = six.moves.configparser.SafeConfigParser()
    build_config.read(find_config_file('kolla-build.conf'))
    config = merge_args_and_config(build_config)
    if config['debug']:
        LOG.setLevel(logging.DEBUG)

    kolla = KollaWorker(config)
    kolla.setup_working_dir()
    kolla.find_dockerfiles()
    kolla.create_dockerfiles()

    if config['template_only']:
        LOG.info('Dockerfiles are generated in {}'.format(kolla.working_dir))
        return

    # We set the atime and mtime to 0 epoch to preserve allow the Docker cache
    # to work like we want. A different size or hash will still force a rebuild
    kolla.set_time()

    queue = kolla.build_queue()

    for x in six.moves.xrange(config['threads']):
        worker = WorkerThread(queue, config)
        worker.setDaemon(True)
        worker.start()

    # block until queue is empty
    queue.join()

    if config['push']:
        for image in kolla.images:
            if image['status'] == "built":
                push_image(image)

    kolla.summary()
    kolla.cleanup()

    return kolla.get_image_statuses()

if __name__ == '__main__':
    main()
