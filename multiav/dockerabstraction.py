from __future__ import print_function

import uuid
import os
import time
import json
import threading
import signal
import datetime

from multiprocessing import Process, Queue, cpu_count
from promise import Promise
from rwlock import RWLock
from threading import Lock
from subprocess import Popen, PIPE, check_output, CalledProcessError, STDOUT

from multiav.enumencoder import EnumEncoder
from multiav.multiactionpromise import MultiActionPromise
from multiav.exceptions import CreateNetworkException, PullPluginException, StartPluginException, CreateDockerMachineMachineException
from multiav.parallelpromise import ParallelPromise

DOCKER_NETWORK_NO_INTERNET_NAME_DEFAULT = "multiav-no-internet-bridge"
DOCKER_NETWORK_INTERNET_NAME_DEFAULT = "multiav-internet-bridge"

class DockerMachine():
    def __init__(self, cfg_parser, engine_classes, max_containers_per_machine, max_scans_per_container, id_overwrite = None, enable_startup_logic = True):
        if id_overwrite:
            self.id = str(id_overwrite)
        else:
            self.id = "multiav-{0}".format(uuid.uuid1()).lower()
        
        self._event_subscribers = dict()
        self.cfg_parser = cfg_parser

        self.engine_classes = engine_classes

        self.max_containers_per_machine = max_containers_per_machine
        self.max_scans_per_container = max_scans_per_container

        self._container_lock = RWLock()
        self._images_lock = dict(map(lambda engine: (engine.name, RWLock()), list(map(lambda engine_class: engine_class(self.cfg_parser), engine_classes))))

        self.containers = []
        self.DOCKER_NETWORK_NO_INTERNET_NAME = self.cfg_parser.gets("MULTIAV", "DOCKER_NETWORK_NO_INTERNET_NAME", DOCKER_NETWORK_NO_INTERNET_NAME_DEFAULT)
        self.DOCKER_NETWORK_INTERNET_NAME = self.cfg_parser.gets("MULTIAV", "DOCKER_NETWORK_INTERNET_NAME", DOCKER_NETWORK_INTERNET_NAME_DEFAULT)

        # starup logic
        if enable_startup_logic:
            self.pull_all_containers()
            self.setup_networks()
            self.remove_running_containers()
            
    def remove_running_containers(self):
        # reindex existing multiav containers
        with self._container_lock.writer_lock:
            containers_to_remove = []
            for container_data in self._list_running_containers():
                # [['malice/floss:latest','floss'], ...]
                if len(container_data) < 2:
                    continue
                
                container_image_name = container_data[0]
                container_id = container_data[1]
                if not "multiav-" in container_id:
                    continue
                
                engine = self._get_engine_from_image_name(container_image_name)
                containers_to_remove.append(container_id)
                print("detected already running multiav container {0} running {1}. removing now to get a clean state...".format(container_id, engine.name))
        
            self.remove_containers(containers_to_remove)

    def setup_networks(self):
        print("Checking if all docker networks exist and creating them if required...")
        if not self.does_no_internet_network_exist():
            print("No-Internet network is not existing. Creating it now...")
            if not self.create_no_internet_network():
                raise CreateNetworkException("Could not create no-internet-network!")
            
            print("No-Internet network created")

        if not self.does_internet_network_exist():
            print("Internet network is not existing. Creating it now...")
            if not self.create_internet_network():
                raise CreateNetworkException("Could not create internet-network!")
            
            print("Internet network created")
    
        print("All networks ok!")

    def pull_all_containers(self):
        print("Checking if all engines are pulled and pulling them if required...")
        pull_promises = dict()
        not_pulled = []

        for engine_class in self.engine_classes:
            # create instance
            engine = engine_class(self.cfg_parser)

            if engine.is_disabled():
                continue
            
            def pull_failed_handler(engine_name):
                not_pulled.append(engine_name)
                print("pull of engine {0} failed!".format(engine_name))

            pull_promise = self.pull_container(engine)
            pull_promise.then(None, lambda engine_name: pull_failed_handler(engine_name))
            pull_promises[engine.name] = pull_promise
        
        # wait for completion
        for engine_name, pull_promise in pull_promises.items():
            #print("waiting for promise {0}".format(engine_name))
            pull_promise.wait()

        if len(not_pulled) != 0:
            raise PullPluginException(" ".join(str(not_pulled)))

        print("All engines pulled!")

    def _rise_event(self, event, *args, **kargs):
        if event in self._event_subscribers:
            for handler in self._event_subscribers[event]:
                handler(*args, **kargs)

    def on(self, event, handler):
        if event in self._event_subscribers:
            self._event_subscribers[event].append(handler)
        else:
            self._event_subscribers[event] = [handler]
    
    def unsubscribe_event_handler(self, event, handler):
        if event in self._event_subscribers:
            self._event_subscribers[event].remove(handler)

    def find_scans_by_file_path(self, file_path):
        scans = []
        for container in self.containers:
            container_scans = container.find_scans_by_file_path(file_path)
            if len(container_scans) != 0:
                scans.extend(container_scans)
            
        return scans

    def find_containers_by_engine(self, engine):
        containers = []
        for container in self.containers:
            # don't return containers marked for removal / stop / restart
            if container.remove_pending or container.restart_pending or container.stop_pending:
                continue

            if container.engine.name == engine.name:
                containers.append(container)
        
        return containers
        
    def _create_container(self, engine, id_overwrite = None, run_now = False):
        try:
            container = DockerContainer(self, engine, self.max_scans_per_container, DOCKER_NETWORK_NO_INTERNET_NAME_DEFAULT, DOCKER_NETWORK_INTERNET_NAME_DEFAULT, id_overwrite = id_overwrite, run_now = run_now)

            if run_now:
                print("Created docker container {0} with engine: {1} on machine {2}".format(container.id, container.engine.name, self.id))
                self.containers.append(container)

            return container
        except Exception as e:
            print("_create_container {0} with engine: {1} on machine {2} Exception: {3}".format(container.id, container.engine.name, self.id, e))
            return None

    def _list_running_containers(self):
        cmd = 'docker ps --filter status=running --format "table {{.Image}}\t{{.Names}}"'
        output = self.execute_command(cmd)
        # [['malice/floss:latest','floss'], ...]
        containers = list(map(lambda x: list(filter(lambda q: q != '',x.split(" "))), output.split("\n")[1:]))
        return containers

    def _get_engine_from_image_name(self, image_name):
        # normalize image name (remove tag)
        image_name = image_name[:image_name.find(":")] if image_name.find(":") != -1 else image_name

        # find class
        for engine_class in self.engine_classes:
            engine = engine_class(self.cfg_parser)
            if "malice/" + engine.container_name == image_name:
                return engine
        
        return None

    def does_no_internet_network_exist(self):
        cmd = "docker network ls"
        output = self.execute_command(cmd)

        return self.DOCKER_NETWORK_NO_INTERNET_NAME in output
    
    def does_internet_network_exist(self):
        cmd = "docker network ls"
        output = self.execute_command(cmd)

        return self.DOCKER_NETWORK_INTERNET_NAME in output

    def create_no_internet_network(self):
        network_address = self.cfg_parser.gets("MULTIAV", "DOCKER_NETWORK_NO_INTERNET", "10.127.139.0")
        cmd = "docker network create --driver bridge --internal --subnet={1}/24 {0}".format(self.DOCKER_NETWORK_NO_INTERNET_NAME, network_address)
        self.execute_command(cmd)

        return self.does_no_internet_network_exist()

    def create_internet_network(self):
        network_address = self.cfg_parser.gets("MULTIAV", "DOCKER_NETWORK_INTERNET", "10.231.101.0")
        cmd = "docker network create --driver bridge --subnet={1}/24 {0}".format(self.DOCKER_NETWORK_INTERNET_NAME, network_address)
        self.execute_command(cmd)

        return self.does_internet_network_exist()

    def create_container(self, engine):
        if len(self.containers) == self.max_containers_per_machine:
            return None
            
        return self._create_container(engine, run_now=True)
    
    def remove_container(self, container):
        # stop will also remove as the container is run with --rm flag
        container.stop()
        if container in self.containers:
            self.containers.remove(container)
        return True
    
    def remove_containers(self, container_ids):
        if len(container_ids) == 0:
            return
        
        # efficient way to stop multiple containers
        cmd = "docker stop {0}".format(" ".join(container_ids))
        output = self.execute_command(cmd, call_super=False)
        return not "error" in output
    
    def pull_container(self, engine):
        container = DockerContainer(self, engine, -1, self.DOCKER_NETWORK_NO_INTERNET_NAME, self.DOCKER_NETWORK_INTERNET_NAME, False)
        return container.pull()
    
    def update(self):
        update_promises = dict()

        # start parallel update of all engines
        for engine_class in self.engine_classes:
            # create instance
            engine = engine_class(self.cfg_parser)

            if engine.is_disabled():
                continue

            # create update container        
            temp_update_container = DockerContainer(self, engine, -1, self.DOCKER_NETWORK_NO_INTERNET_NAME, self.DOCKER_NETWORK_INTERNET_NAME, False)
            temp_update_container.id = "multiav-" + engine.container_name + "-updater"    

            # run update
            engine_update_promise = temp_update_container.update()

            # cleanup
            engine_update_promise.then(
              lambda res: temp_update_container.remove()
            )
            update_promises[engine] = engine_update_promise

        update_promise = MultiActionPromise(update_promises)

        # schedule each container to stop => will get updates when started again
        def post_update_cleanup_function():
            print("post_update_cleanup_function: max_scans_per_container: {0}".format(self.max_scans_per_container))
            print("post_update_cleanup_function len containers {0}".format(len(self.containers)))
            for container in self.containers:
                try:
                    container.restart()
                except Exception as e:
                    print("post_update_cleanup_function exception: {0}".format(e))
        
        update_promise.then(lambda res: post_update_cleanup_function())

        return update_promise

    def execute_command(self, command, call_super = False):
        print("--execute command: " + command)
        '''try: 
            # close_fds=True ?
            process = subprocess.call(command, stdout=PIPE, close_fds=True)
            output = process.communicate()[0].strip()
            return output
        except Exception as e:
            print("Exception while executing command: {0} response: {2} exception: {1}".format(command, e, response))
            return'''
        try:
            output = check_output(command.split(" "), stderr=STDOUT)
        except CalledProcessError as e:
            output = e.output
        
        return str(output.decode("utf-8"))

    def try_do_scan(self, engine, file_path):
        # abstract
        pass
    
class LocalDynamicDockerMachine(DockerMachine):
    def __init__(self, cfg_parser, engine_classes, max_containers_per_machine, max_scans_per_container, id_overwrite = None):
        DockerMachine.__init__(self, cfg_parser, engine_classes, max_containers_per_machine, max_scans_per_container, id_overwrite = id_overwrite)

        if len(self.containers) > max_containers_per_machine:
            print("found running containers on this docker machine. Stopping them now to get a clean state...")
            for container in self.containers[max_containers_per_machine:]:
                self.remove_container(container)
                print("stopped container {0} running engine {1}".format(container.id, container.engine.name))
    
    def try_do_scan(self, engine, file_path):
        containers = self.find_containers_by_engine(engine)
        
        if len(containers) == 0:
            container = self._create_container(engine, run_now=True)
            if not container.try_do_scan(file_path):
                return None, None
            
            return container, self
        
        if not containers[0].try_do_scan(file_path):
            return None, None
        
        return containers[0], self

class LocalStaticDockerMachine(DockerMachine):
    def __init__(self, cfg_parser, engine_classes, id_overwrite = None):
        DockerMachine.__init__(self, cfg_parser, engine_classes, max_containers_per_machine = -1, max_scans_per_container = -1, id_overwrite = id_overwrite)

        print("Checking if all plugins are running and staring them if required...")
        for engine_class in self.engine_classes:
            #create instance
            engine = engine_class(self.cfg_parser)

            if engine.is_disabled():
                continue
            
            # check if new instance must be created
            containers_running_this_engine = self.find_containers_by_engine(engine)

            if len(containers_running_this_engine) == 0:
                # create container instance
                container = self._create_container(engine, run_now=True)
            elif len(containers_running_this_engine) == 1:
                # there's already a container running this engine
                print("WARNING: reusing already running container {0} with engine {1}. Assuming no scans are running on this one...".format(containers_running_this_engine[0].id, containers_running_this_engine[0].engine.name))
                pass
            else:
                # too many containers running this engine, stop all except one
                print("found too many containers running engine {0}. Stopping all except one...".format(engine.name))
                for container in containers_running_this_engine[1:]:
                    container.remove()
                    self.containers.remove(container)
                    print("stopped container {0} running engine {1}".format(container.id, container.engine.name))
        
        print("All plugins started!")
    
    def create_container(self, engine):
        print("create container not supported")

    def remove_container(self, container):
        print("remove container not supported")
    
    def try_do_scan(self, engine, file_path):
        containers = self.find_containers_by_engine(engine)
        if len(containers) == 0:
            # container was probably marked for removal / stop & / restart => create new one
            container = self._create_container(engine, run_now=True)
            if not container.try_do_scan(file_path):
                return None, None
            
            return container, self
        
        if not containers[0].try_do_scan(file_path):
            return None, None
        
        return containers[0], self

class DockerMachineMachine(DockerMachine):
    def __init__(self, cfg_parser, engine_classes, max_containers_per_machine, max_scans_per_container, create_machine = True, minimal_machine_run_time = 400, id_overwrite = None, execute_startup_checks = True, never_shutdown = False):
        DockerMachine.__init__(self, cfg_parser, engine_classes, max_containers_per_machine, max_scans_per_container, id_overwrite=id_overwrite, enable_startup_logic=False)
        self.max_scans_per_container = max_scans_per_container
        self.max_containers_per_machine = max_containers_per_machine
        self.minimal_machine_run_time = minimal_machine_run_time
        self.never_shutdown = never_shutdown
        self._shutdown_check_backoff = None
        self._shutdown_check_last_date = None

        # values from config file
        self.cmd_docker_machine_create = cfg_parser.gets("MULTIAV", "CMD_DOCKER_MACHINE_CREATE", None)

        # startup logic
        if create_machine:
            if not self._create_machine():
                raise CreateDockerMachineMachineException("could not create machine!")
        
        # do this manually as we may have to start the machine first
        if execute_startup_checks:
            self.pull_all_containers()
            self.setup_networks()
            self.remove_running_containers()

        # create shutdown check promise
        if not never_shutdown:
            self._schedule_shutdown_check()

    def _schedule_shutdown_check(self):
        def fn():
            self._shutdown_check_backoff = 1
            
            while True:
                time.sleep(self.minimal_machine_run_time ** self._shutdown_check_backoff)

                if self.try_shutdown():
                    return
                self._shutdown_check_last_date = datetime.datetime.now()
                self._shutdown_check_backoff += 1

        self._shutdown_check_thread = threading.Thread(target=fn)
        self._shutdown_check_thread.daemon = True
        self._shutdown_check_thread.start()
    
    def try_shutdown(self):
        can_shutdown = True
        for container in self.containers:
            if len(container.scans) != 0:
                can_shutdown = False
                break
        
        if not can_shutdown:
            # resschedule check
            print("try_shutdown: can not shut down machine.")
            return False

        if not self._remove_machine():
            print("try_shutdown: shutdown failed!!")
            return False
        
        return True

    def _create_machine(self):
        cmd = "docker-machine create --driver {0} {1}".format(self.cmd_docker_machine_create, self.id)

        # must be executed without env
        output = DockerMachine.execute_command(self, cmd)

        result = "Docker is up and running!" in output

        # rise event
        if result:
            self._rise_event("machine_started", self)
        
        return result
    
    def _remove_machine(self):
        cmd = "docker-machine rm -f {0}".format(self.id)

        # must be executed without env
        output = DockerMachine.execute_command(self, cmd)
        result = "Successfully removed" in output

        # rise event
        if result:
            self._rise_event("shutdown", self)

        return result
        
    def execute_command(self, command, call_super = False):
        # execute command
        if call_super:
            output = DockerMachine.execute_command(self, command)
        else:
            if command.find("&&") != -1:
                command = "\"" + command + "\""
            
            cmd = "docker-machine ssh {0} sudo {1}".format(self.id, command)
            output = DockerMachine.execute_command(self, cmd)

        return output

    def try_do_scan(self, engine, file_path):
        if self.max_scans_per_container == 1:
            # do we have the resources to add a new container?
            if len(self.containers) == self.max_containers_per_machine:
                print("No ressources to start container with engine {0} on machine {1}".format(engine.name, self.id))
                return None, None

            container = self._create_container(engine, run_now=True)
            if container is None:
                raise Exception("Could not create container with engine {0} on machine {1}".format(engine.name, self.id))

            if not container.try_do_scan(file_path):
                return None, None
            
            return container, self
        
        # multiple scans per container are allowed
        if len(self.containers) != 0:
            # check if we can use a running container
            containers = self.find_containers_by_engine(engine)
            for container in containers:
                if container.try_do_scan(file_path):
                    print("using container for multiple scans")
                    return container, self
            
        # create a new container for scan
        container = self._create_container(engine, run_now=True)
        if not container.try_do_scan(file_path):
            return None, None
        
        return container, self

class DockerContainer():
    def __init__(self, machine, engine, max_scans_per_container, network_no_internet_name, network_internet_name, id_overwrite = None, run_now = False):
        if id_overwrite:
            self.id = id_overwrite
        else:
            self.id = "multiav-{0}-{1}".format(engine.name, uuid.uuid1()).lower()
        
        self.machine = machine
        self.engine = engine
        self.scans = []
        self.max_scans_per_container = max_scans_per_container

        self.network_no_internet_name = network_no_internet_name
        self.network_internet_name = network_internet_name

        self.restart_pending = False
        self.stop_pending = False
        self.remove_pending = False

        if run_now:
            if not self.run():
                raise Exception("Container run failed")
        
    def _get_tag(self):
        cmd = "docker images malice/{0}:updated".format(self.engine.container_name)
        output = self.machine.execute_command(cmd)

        return "updated" if self.engine.container_name in output else "latest"
    
    def _check_pending_actions(self):
        if len(self.scans) != 0:
            return

        if self.remove_pending:
            self.remove()
            return

        if self.restart_pending:
            self.restart()
            return
        
        if self.stop_pending:
            self.stop()
            return        

    def pull(self):
        # pull function wrapped in promise
        def pull_wrapper(resolve, reject):
            if self.is_pulled():
                print("Container {0} already pulled on machine {1}".format(self.engine.name, self.machine.id))
                resolve(self.engine.name)
                return

            if not self._pull():
                print("Container {0} pull on machine {1} FAILED!".format(self.engine.name, self.machine.id))
                reject(Exception("_pull failed for engine {0}".format(self.engine.name)))
                return
            
            resolve(self.engine.name)
            return
        
        return ParallelPromise(lambda resolve, reject: pull_wrapper(resolve, reject))

    def _pull(self, set_active=True):
        # seriell pull function
        try:
            # pull or build new container
            if self.engine.container_build_url_override != None or len(self.engine.container_build_params) != 0:
                container_url = ""
                if self.engine.container_build_url_override:
                    container_url = self.engine.container_build_url_override
                else:
                    container_url = "https://github.com/malice-plugins/{0}.git".format(self.engine.container_name)
            
                print("building docker container malice/{0}:latest from url: {1}".format(self.engine.container_name, container_url))
                cmd = "docker build --tag malice/{0}:latest$BUILDARGS$ {1}".format(self.engine.container_name, container_url)

                # set build params (e.g license keys)
                if len(self.engine.container_build_params) == 0:
                    cmd = cmd.replace("$BUILDARGS$", "")
                else:
                    cmd = cmd.replace("$BUILDARGS$", "".join(map(lambda kv: " --build-arg " + kv[0] + "=" + kv[1], self.engine.container_build_params.items())))

                output = self.machine.execute_command(cmd)

                if not "Successfully built" in output:
                    print(output)
                    return False

                print("Built container for plugin {0} successfully!".format(self.engine.container_name))
            else:
                print("pulling docker container malice/{0}".format(self.engine.container_name))
                cmd = "docker pull malice/{0}".format(self.engine.container_name)

                output = self.machine.execute_command(cmd)

                if not ("Status: Downloaded newer image" in output or "Status: Image is up to date" in output):
                    print(output)
                    return False
                
                print("Pulled container for plugin {0} successfully!".format(self.engine.container_name))

            # any additional files required?
            if len(self.engine.container_additional_files) != 0:
                print("Container {0} requires additional files. Handling now...".format(self.engine.name))
                dest_dir = self.machine.execute_command("pwd").rstrip()
                for additional_file in self.engine.container_additional_files:
                    additional_file_name = os.path.basename(additional_file)
                    cmd = "docker-machine scp {0} {1}:{2}/{3}".format(additional_file, self.machine.id, dest_dir, additional_file_name)
                    output = self.machine.execute_command(cmd, call_super = True)

                    # check copy success
                    if "scp: " in output:
                        print("Container {0} copy additional file {2} to machine {1} FAILED!".format(self.engine.name, self.machine.id, additional_file))
                        print(output)
                        return False
                    
                    print("Copied file {0} for container {1} to machine {2} successfully!".format(additional_file_name, self.engine.name, self.machine.id))
            
            # set image active by renaming to current
            if set_active:
                with self.machine._images_lock[self.engine.name].writer_lock:
                    # remove old image
                    cmd = "docker rmi malice/{0}:old".format(self.engine.container_name)
                    output = self.machine.execute_command(cmd)
                    if "Error" in output and not "No such image" in output:
                        print("Container {0} on machine {1} remove :old image FAILED!".format(self.engine.name, self.machine.id))
                        print(output)
                        return False

                    # rename current to old
                    if not self.retag("current", "old"):
                        print("Container {0} on machine {1} retag :current image to :old FAILED!".format(self.engine.name, self.machine.id))
                        return False

                    # rename latest to current 
                    cmd = "docker tag malice/{0}:latest malice/{0}:current".format(self.engine.container_name)
                    output = self.machine.execute_command(cmd)
                    if "Error" in output:
                        print("Container {0} on machine {1} retag :latest image to :current FAILED!".format(self.engine.name, self.machine.id))
                        print(output)
                        return False
            
            print("Update of plugin {0} on machine {1} successs!".format(self.engine.name, self.machine.id))
            return True
        except Exception as e:
            print("Update of plugin {0} on machine {1} failed! Exception: {2}".format(self.engine.name, self.machine.id, e))
            return False

    def find_scans_by_file_path(self, file_path):
        scans =  []
        for scan in self.scans:
            if scan == file_path:
                scans.append((self, file_path))
        return scans

    def run(self):
        with self.machine._images_lock[self.engine.name].reader_lock:
            network_name = self.network_internet_name if self.engine.container_requires_internet else self.network_no_internet_name
            cmd = "docker run -d --name {0} --net {1}$DOCKERPARAMS$ --rm malice/{2}:current$CMDARGS$ web".format(self.id, network_name, self.engine.container_name)

            # set docker parameters
            if len(self.engine.container_run_docker_parameters) == 0:
                cmd = cmd.replace("$DOCKERPARAMS$", "")
            else:
                cmd = cmd.replace("$DOCKERPARAMS$", " " + " ".join(
                    map(lambda kv: kv[0] + "=" + kv[1], self.engine.container_run_docker_parameters.items())))
                
            # set command arguments
            if len(self.engine.container_run_command_arguments) == 0:
                cmd = cmd.replace("$CMDARGS$", "")
            else:
                cmd = cmd.replace("$CMDARGS$", " " + " ".join(map(lambda kv: kv[0] + "=" + kv[1], self.engine.container_run_command_arguments.items())))
            
            # start
            try:
                output = self.machine.execute_command(cmd)

                if "Error" in output:
                    raise Exception(output)

                # give the container a sec to start
                time.sleep(1)

                return True
            except Exception as e:
                print(cmd)
                print(e)
                return False
    
    def remove(self):
        if len(self.scans) != 0:
            self.remove_pending = True
            return True
        
        cmd = "docker rm {0}".format(self.id)
        output = self.machine.execute_command(cmd)

        return self.engine.container_name in output
    
    def is_pulled(self):
        #latest must always exist
        cmd = "docker images malice/{0}:current".format(self.engine.container_name)
        output = self.machine.execute_command(cmd)
        res = self.engine.container_name in output
        return res

    def is_running(self):
        cmd = "docker ps --filter status=running"
        output = self.machine.execute_command(cmd)
        return self.id in output

    def stop(self):
        if len(self.scans) != 0:
            self.stop_pending = True
            return True
        
        cmd = "docker stop {0}".format(self.id)
        output = self.machine.execute_command(cmd)
        return self.id in output

    def restart(self):
        if len(self.scans) != 0:
            self.restart_pending = True
            print("{0} restart scheduled".format(self.id))
            return True

        print("{0} restarting".format(self.id))
        if self.stop():
            return self.run()
        
        return False
    
    def try_do_scan(self, file_path):
        if self.max_scans_per_container != -1:
            if len(self.scans) >= self.max_scans_per_container:
                return False
            
        self.scans.append(file_path)
        return True
    
    def remove_scan(self, file_path):
        if not file_path in self.scans:
            self._check_pending_actions()
            return False

        self.scans.remove(file_path)
        self._check_pending_actions()
        return True

    def get_container_version(self):
        cmd = "docker run --rm malice/{0}:current --version".format(self.engine.container_name)

        # e.g: floss version v0.1.0, BuildTime: 20190209
        output = self.machine.execute_command(cmd)  
        
        version = output[output.find("version v") + len("version v"):output.find(",")]
        buildtime = output[output.find("BuildTime: ") + len("BuildTime: "):]

        # e.g. pescan only returns v0.1.0
        if len(version) == 0:
            version = output
        
        # replace empty results with dash
        if len(version) == 0:
            version = "-"
        if len(buildtime) == 0:
            buildtime = "-"

        return version, buildtime

    def get_signature_version(self):
        cmd = "docker run --rm --entrypoint cat malice/{0}:current /opt/malice/UPDATED".format(self.engine.container_name)
        output = self.machine.execute_command(cmd)

        if "No such file or directory" in output or "No such container" in output:
            return "-"
        return output
    
    def retag(self, old_tag, new_tag):
        # add new tag
        cmd = "docker tag malice/{0}:{1} malice/{0}:{2}".format(self.engine.container_name, old_tag, new_tag)
        output = self.machine.execute_command(cmd)

        if "Error" in output and not "No such image" in output:
            print(output)
            return False

        # remove old tag
        cmd = "docker rmi malice/{0}:{1}".format(self.engine.container_name, old_tag)
        output = self.machine.execute_command(cmd)

        
        if "Error" in output and not "No such image" in output:
            print(output)
            return False
        return True

    def update(self):
        def update_wrapper_function(resolve, reject):
            try:
                resolve(json.dumps(self._update(), cls=EnumEncoder))
            except Exception as e:
                print("exception in update_function: {0}".format(e))
                reject()

        return ParallelPromise(lambda resolve, reject: update_wrapper_function(resolve, reject))

    def _update(self):
        try:
            # make sure container is running to get pre update signature version
            old_signature_version = self.get_signature_version()

            # give docker some time to stop the container
            time.sleep(2)

            # get old container version info
            old_container_version, old_container_build_time = self.get_container_version()

            # pull container
            new_container_version = old_container_version
            if self.engine.update_pull_supported:
                # remove old latest image
                '''with self.machine._images_lock[self.engine.name].writer_lock:
                    cmd = "docker rmi malice/{0}:latest".format(self.engine.container_name)
                    output = self.machine.execute_command(cmd)
                    if "Error" in output and not "No such image" in output:
                        raise Exception("[{0}] Update failed. Could not remove old latest image!".format(self.engine.container_name))'''

                # Check for docker image update on the store
                if not self._pull(set_active=False):
                    print("[{0}] Update failed. Could not pull container!".format(self.engine.container_name))
                    raise Exception("pull failed")
                
                # Get container version info
                new_container_version, new_container_build_time = self.get_container_version()

            # skip update call if not supported => updates via pull only
            new_signature_version = old_signature_version

            # cleanup :old and :update image
            try:
                '''cmd = "docker images malice/{0}:updated".format(self.engine.container_name)
                output = self.machine.execute_command(cmd)

                if self.engine.container_name in output:'''
                cmd = "docker rmi malice/{0}:updated malice/{0}:old".format(self.engine.container_name)
                output = self.machine.execute_command(cmd)
            except Exception as e:
                print("[{0}] docker images / rmi exception {1}".format(self.engine.container_name, e))
                raise Exception("remove :old and :updated image failed")
                
            if self.engine.update_command_supported:
                # run new container to do the update
                try:
                    cmd = "docker run --name {0} malice/{1}:latest update".format(self.id, self.engine.container_name)
                    output = self.machine.execute_command(cmd)
                except Exception as e:
                    print("[{0}] Docker run update command exception {1}".format(self.engine.container_name, e))
                    raise Exception("create update container failed")
                

                # save updated container as new image with tag updated
                try:
                    cmd = "docker commit {0} malice/{1}:updated".format(self.id, self.engine.container_name)
                    output = self.machine.execute_command(cmd)
                except Exception as e:
                    print("[{0}] Docker commit exception {1}".format(self.engine.container_name, e))
                    self.remove()
                    raise Exception("Commit :updated inage failed")
                
                # remove the container used for updating
                if not self.remove():
                    print("[{0}] {1}".format(self.engine.container_name, "could not remove the updated container"))
                    raise Exception("remove updated container failed")
                
                # rename current image to old and updated image to current
                try:
                    with self.machine._images_lock[self.engine.name].writer_lock:
                        if not self.retag("current", "old"):
                            raise Exception("rename current to old failed")

                        if not self.retag("updated", "current"):
                            raise Exception("rename updated to current failed")
                except Exception as e:
                    print("[{0}] docker images / rmi exception {1}".format(self.engine.container_name, e))
                    raise e

                # run a container to get signature version
                new_signature_version = self.get_signature_version()
            return {
                'engine': self.engine.name,
                'status': "success",
                'old_signature_version': old_signature_version,
                'old_container_build_time': old_container_build_time,
                'signature_version': new_signature_version,
                'container_build_time': new_container_build_time,
                'container_version': new_container_version,
                'plugin_type': self.engine.plugin_type,
                'has_internet': self.engine.container_requires_internet,
                'speed': self.engine.speed.name
            }
        except Exception as e:
            return {
                'engine': self.engine.name,
                'status': "error: {0}".format(e),
                'old_signature_version': old_signature_version,
                'old_container_build_time': old_container_build_time,
                'signature_version': new_signature_version,
                'container_build_time': new_container_build_time,
                'container_version': new_container_version,
                'plugin_type': self.engine.plugin_type,
                'has_internet': self.engine.container_requires_internet,
                'speed': self.engine.speed.name
            }