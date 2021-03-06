# description     : Script for helping with the rebuild of container images.
# author          : pkubat@redhat.com
# notes           : Rewritten from a shell script originally created by hhorak@redhat.com.
# python_version  : 3.x

import subprocess
import os
import shutil
import re
import tempfile
import pprint
import getpass
import logging
from copy import copy

from git import Repo

import container_workflow_tool.utility as u
from container_workflow_tool.koji import KojiAPI
from container_workflow_tool.distgit import DistgitAPI
from container_workflow_tool.utility import RebuilderError
from container_workflow_tool.decorators import needs_base, needs_brewapi, needs_dhapi
from container_workflow_tool.decorators import needs_distgit
from container_workflow_tool.config import Config


class ImageRebuilder:
    """Class for rebuilding Container images."""

    def __init__(self, base_image, rebuild_reason=None, config="default.yaml", release="current"):
        """ Init method of ImageRebuilder class

        Args:
            base_image (str): image id to be used as a base image
            config (str, optional): configuration file to be used
            rebuild_reason (str, optional): reason for the rebuild,
                                            used in commit
        """
        self.base_image = base_image

        self.brewapi = None
        self.dhapi = None
        self.distgit = None
        self.commit_msg = None
        self.args = None
        self.tmp_workdir = None
        self.repo_url = None
        self.jira_header = None

        self.conf_name = config
        self.rebuild_reason = rebuild_reason
        self.do_image = None
        self.exclude_image = None
        self.do_set = None
        self.check_script = None
        self.image_set = None
        self.disable_klist = None
        self.latest_release = None

        self._setup_logger()
        self.set_config(self.conf_name, release=release)

    @classmethod
    def from_args(cls, args):
        """
        Creates an ImageRebuilder instance from argparse arguments.
        """
        rebuilder = ImageRebuilder(base_image=args.base)
        rebuilder._setup_args(args)
        return rebuilder

    def _setup_args(self, args):
        self.args = args

        if args.config:
            conf = args.config.split(':')
            config_fn = conf[0]
            image_set = conf[1] if len(conf) > 1 else 'current'
            self.set_config(config_fn, image_set)
        if args.tmp:
            self.set_tmp_workdir(args.tmp)
        if args.clear_cache:
            self.clear_cache()
        if args.do_image:
            self.set_do_images(args.do_image)
        if args.exclude_image:
            self.set_exclude_images(args.exclude_image)
        if args.do_set:
            self.set_do_set(args.do_set)
        self.logger.setLevel(u._transform_verbosity(args.verbosity))

        # Command specific
        # TODO: generalize?
        if getattr(args, 'repo_url', None) is not None and args.repo_url:
            self.set_repo_url(args.repo_url)
        if getattr(args, 'commit_msg', None) is not None:
            self.set_commit_msg(args.commit_msg)
        if getattr(args, 'rebuild_reason', None) is not None and args.rebuild_reason:
            self.rebuild_reason = args.rebuild_reason
        if getattr(args, 'check_script', None) is not None and args.check_script:
            self.check_script = args.check_script
        if getattr(args, 'disable_klist', None) is not None and args.disable_klist:
            self.disable_klist = args.disable_klist
        if getattr(args, 'latest_release', None) is not None and args.latest_release:
            self.latest_release = args.latest_release

        # Image set to build
        if getattr(args, 'image_set', None) is not None and args.image_set:
            self.image_set = args.image_set

    def _get_set_from_config(self, layer):
        i = getattr(self.conf, layer, [])
        if i is None:
            err_msg = "Image set '{}' not found in config.".format(layer)
            raise RebuilderError(err_msg)
        return i

    def _setup_distgit(self):
        if not self.distgit:
            self.distgit = DistgitAPI(self.base_image, self.conf,
                                      self.rebuild_reason, copy(self.logger))

    def _setup_brewapi(self):
        if not self.brewapi:
            self.brewapi = KojiAPI(self.conf, copy(self.logger),
                                   self.latest_release)

    def _setup_dhapi(self):
        from dhwebapi.dhwebapi import DockerHubWebAPI, DockerHubException
        if not self.dhapi:
            token = None
            username = None
            password = None
            try:
                token = self.conf.DOCKERHUB_TOKEN
                self.dhapi = DockerHubWebAPI(token=token)
                return
            except (AttributeError, DockerHubException):
                pass

            try:
                username = self.conf.DOCKERHUB_USERNAME
                password = self.conf.DOCKERHUB_PASSWORD
            except AttributeError:
                if username is None:
                    username = input("Dockerhub username: ")
                if password is None:
                    password = getpass.unix_getpass(prompt="Password for user " + username + ": ")

            self.dhapi = DockerHubWebAPI(username, password)

    def _setup_logger(self, level=logging.INFO, user_logger=None):
        # If a logger has been provided, do not setup own
        if user_logger and isinstance(user_logger, logging.Logger):
            logger = user_logger
        else:
            logger = u.setup_logger("main", level)

        self.logger = logger
        return logger

    def _check_kerb_ticket(self):
        if not self.disable_klist:
            ret = subprocess.run(["klist"], stdout=subprocess.DEVNULL)
            if ret.returncode:
                raise(RebuilderError("Kerberos token not found."))

    def _change_workdir(self, path):
        self.logger.info("Using working directory: " + path)
        os.chdir(path)

    @needs_base
    def _get_tmp_workdir(self, setup_dir=True):
        # Check if the workdir has been set by the user
        if self.tmp_workdir:
            return self.tmp_workdir
        tmp = None
        tmp_id = self.base_image.replace(':', '-')
        # Check if there is an existing tempdir for the build
        for f in os.scandir(tempfile.gettempdir()):
            if os.path.isdir(f.path) and f.name.startswith(tmp_id):
                tmp = f.path
                break
        else:
            if setup_dir:
                tmp = tempfile.mkdtemp(prefix=tmp_id)
        return tmp

    def set_do_images(self, val):
        self.do_image = val

    def set_exclude_images(self, val):
        self.exclude_image = val

    def set_do_set(self, val):
        self.do_set = val

    def _get_images(self):
        images = []
        if self.do_set:
            # Use only the image sets the user asked for
            for layer in self.do_set:
                images += self._get_set_from_config(layer)
        else:
            # Go through all known layers and create a single image list
            for (order, layer) in self.conf.layers.items():
                i = getattr(self.conf, layer, [])
                images += i
        return self._filter_images(images)

    def _filter_images(self, base):
        if self.do_image:
            return [i for i in base if i["component"] in self.do_image]
        elif self.exclude_image:
            return [i for i in base if i["component"] not in self.exclude_image]
        else:
            return base

    def _prebuild_check(self, image_set, branches=[]):
        tmp = self._get_tmp_workdir(setup_dir=False)
        if not tmp:
            raise RebuilderError("Temporary directory structure does not exist. Pull upstream first.")
        self.logger.info("Checking for correct repository configuration ...")
        releases = branches
        for image in image_set:
            component = image["component"]
            cwd = os.path.join(tmp, component)
            try:
                repo = Repo(cwd)
            except GitError as e:
                self.logger.error("Failed to open repository for {}", component)
                raise e
            # This checks if any of the releases can be found in the name of the checked-out branch
            if releases and not [i for i in releases if i in str(repo.active_branch)]:
                raise RebuilderError("Unexpected active branch for {}: {}".format(component,
                                                                                  repo.active_branch))

    def _build_images(self, image_set, custom_args=[], branches=[]):
        if not image_set:
            # Nothing to build
            self.logger.warn("No images to build, exiting.")
            return
        if not branches:
            # Fill defaults from config if not provided
            for release in self.conf.releases:
                branches += [self.conf.releases[release]["current"]]
        self._prebuild_check(image_set, branches)

        procs = []
        tmp = self._get_tmp_workdir(setup_dir=False)
        for image in image_set:
            component = image["component"]
            cwd = os.path.join(tmp, component)
            self.logger.info("Building image {} ...".format(component))
            args = [u._get_packager(self.conf), 'container-build']
            if custom_args:
                args.extend(custom_args)
            proc = subprocess.Popen(args, cwd=cwd, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE,
                                    universal_newlines=True)
            # Append the process and component information for later use
            procs.append((proc, component))

        self.logger.info("Fetching tasks...")
        for proc, component in procs:
            self.logger.debug("Query component: {}".format(component))
            # Iterate until a taskID is found
            for stdout in iter(proc.stdout.readline, ""):
                if "taskID" in stdout:
                    self.logger.info("{} - {}".format(component,
                                                      stdout.strip()))
                    break
            else:
                # If we get here the command must have failed
                # The error will get printed out later when getting all builds
                temp = "Could not find task for {}!"
                self.logger.warning(temp.format(component))

        self.logger.info("Waiting for builds...")
        timeout = 30
        while procs:
            self.logger.debug("Looping over all running builds")
            for proc, image in procs:
                out = err = None
                try:
                    self.logger.debug("Waiting {} seconds for {}".format(timeout,
                                                                         image))
                    out, err = proc.communicate(timeout=timeout)
                except subprocess.TimeoutExpired:
                    msg = "{} not yet finished, checking next build"
                    self.logger.debug(msg.format(image))
                    continue

                self.logger.info("{} build has finished".format(image))
                if err:
                    # Write out stderr if we encounter an error
                    err = u._4sp(err)
                    self.logger.error(err)
                procs.remove((proc, image))

    def _get_config_path(self, config):
        if not os.path.isabs(config):
            base_path = os.path.abspath(__file__)
            dir_path = os.path.dirname(base_path)
            path = os.path.join(dir_path, "config/", config)
        else:
            path = config
        return path

    def _not_yet_implemented(self):
        print("Method not yet implemented.")

    @needs_brewapi
    def get_brew_builds(self, print_time=True):
        """Returns information about builds in brew

        Args:
            print_time (bool, optional): Print time finished for a build.

        Returns:
            str: Resulting brew build text
        """
        output = []
        header = "||Component||Build||Image_name||"
        if print_time:
            header += "Build finished||"
        header += "Archives||"
        output.append(header)
        nvrs = (self.brewapi.get_nvrs(self._get_images()))
        for item in nvrs:
            nvr, name, component, *rest = item
            # No nvr found for the image, might not have been built
            if nvr is None:
                continue
            else:
                template = "|{0}|{1}|{2}|"
            vr = re.search(".*-([^-]*-[^-]*)$", nvr).group(1)
            build_id = self.brewapi.get_buildinfo(nvr)["build_id"]
            archives = self.brewapi.brew.listArchives(build_id)
            archive = archives[0]["extra"]
            name = archive["docker"]["config"]["config"]["Labels"]["name"]
            image_name = "{name}:{vr}".format(name=name, vr=vr)
            result = template.format(component, nvr, image_name)
            if print_time:
                result += self.brewapi.get_time_built(nvr) + '|'
            result += str(len(archives))
            output.append(result)
        return '\n'.join(output)

    def set_config(self, conf_name, release="current"):
        """
        Use a configuration file other than the current one.
        The configuration file used must be located in the standard 'config' directory.

        Args:
            config(str): Name of the configuration file (filename)
            release(str, optional): ID of the release to be used inside the config
        """
        path = self._get_config_path(conf_name)
        self.logger.debug("Setting config to {}", path)
        with open(path) as f:
            newconf = Config(f, release)
        self.conf = newconf
        # Set config for every module that is set up
        if self.brewapi:
            self.brewapi.conf = newconf
        if self.distgit:
            self.distgit.conf = newconf

    def set_tmp_workdir(self, tmp):
        """
        Sets the temporary working directory to the one provided.
        The directory has to already exist.

        Args:
            tmp(str): location of the directory to be used
        """
        if os.path.isdir(tmp):
            self.tmp_workdir = os.path.abspath(tmp)
        else:
            raise RebuilderError("Provided working directory does not exist.")

    @needs_distgit
    def set_commit_msg(self, msg):
        """
        Set the commit message to some other than the default one.

        Args:
            msg(str): Message to be written into the commit.
        """
        self.distgit.set_commit_msg(msg)

    def clear_cache(self):
        """Clears various caches used in the rebuilding process"""

        self.logger.info("Removing cached data and git storage.")
        # Clear ondisk storage for git and the brew cache
        tmp = self._get_tmp_workdir(setup_dir=False)
        shutil.rmtree(tmp, ignore_errors=True)
        # If the working directory has been set by the user, recreate it
        if self.tmp_workdir:
            os.makedirs(tmp)

        # Clear koji object caches
        self.nvrs = []
        if self.brewapi:
            self.brewapi.clear_cache()

    def set_repo_url(self, repo_url):
        """Repofile url setter

        Sets the url of .repo file used for the build.

        Args:
            repo_url: url of .repo file used for the build
        """
        self.repo_url = repo_url

    def list_images(self):
        """Prints list of images that we work with"""
        for i in self._get_images():
            print(i["component"])

    def print_upstream(self):
        """Prints the upstream name and url for images used in config"""
        template = "{component} {img_name} {ups_name} {url}"
        for i in self._get_images():
            ups_name = re.search(".*\/([a-zA-Z0-9-]+).git",
                                 i["git_url"]).group(1)
            print(template.format(component=i["component"], url=i["git_url"],
                                  ups_name=ups_name, img_name=i["name"]))

    def show_config_contents(self):
        """Prints the symbols and values of configuration used"""
        for key in self.conf:
            value = getattr(self.conf, key)
            # Do not print clutter the output with unnecessary content
            if key in ["raw"]:
                continue
            print(key + ":")
            pprint.pprint(value, compact=True, width=256, indent=4)

    def build_images(self, image_set=None):
        """
        Build images specified by image_set (or self.image_set)
        """
        if image_set is None and self.image_set is None:
            raise RebuilderError("image_set is None, build cancelled.")
        if image_set is None:
            image_set = self.image_set
        image_config = self._get_set_from_config(image_set)
        images = self._filter_images(image_config)
        self._build_images(images)

    def print_brew_builds(self, print_time=True):
        """Prints information about builds in brew

        Args:
            print_time (bool, optional): Print time finished for a build.

        Returns:
            str: Resulting brew build text
        """
        print(self.get_brew_builds(print_time=print_time))

    # Dist-git method wrappers
    @needs_distgit
    def pull_downstream(self):
        """Pulls downstream dist-git repositories and does not make any further changes to them

        Additionally runs a script against each repository if check_script is set, checking its exit value.
        """
        self._check_kerb_ticket()
        tmp = self._get_tmp_workdir()
        self._change_workdir(tmp)
        images = self._get_images()
        for i in images:
            self.distgit._clone_downstream(i["component"], i["git_branch"])
        # If check script is set, run the script provided for each config entry
        if self.check_script:
            for i in images:
                self.distgit.check_script(i["component"], self.check_script,
                                          i["git_branch"])

    @needs_distgit
    def pull_upstream(self):
        """Pulls upstream git repositories and does not make any further changes to them

        Additionally runs a script against each repository if check_script is set, checking its exit value.
        """
        tmp = self._get_tmp_workdir()
        self._change_workdir(tmp)
        images = self._get_images()
        for i in images:
            # Use unversioned name as a path for the repository
            ups_name = i["name"].split('-')[0]
            repo = self.distgit._clone_upstream(i["git_url"],
                                                ups_name,
                                                commands=i["commands"])
        # If check script is set, run the script provided for each config entry
        if self.check_script:
            for i in images:
                ups_name = i["name"].split('-')[0]
                self.distgit.check_script(i["component"], self.check_script,
                                          os.path.join(ups_name, i["git_path"]))

    @needs_distgit
    def push_changes(self):
        """Pushes changes for all components into downstream dist-git repository"""
        # Check for kerberos ticket
        self._check_kerb_ticket()
        tmp = self._get_tmp_workdir(setup_dir=False)
        if not tmp:
            raise RebuilderError("Temporary directory structure does not exist. Pull upstream/rebase first.")
        self._change_workdir(tmp)
        images = self._get_images()

        self.distgit.push_changes(tmp, images)

    def dist_git_rebase(self):
        """
        Do a rebase against a new base/s2i image.
        Does not pull in upstream changes of layered images.
        """
        self.dist_git_changes(rebase=True)

    @needs_distgit
    def dist_git_changes(self, rebase=False):
        """Method to merge changes from upstream into downstream

        Pulls both downstream and upstream repositories into a temporary directory.
        Merge is done by copying tracked files from upstream into downstream.

        Args:
            rebase (bool, optional): Specifies whether a rebase should be done instead.
        """
        # Check for kerberos ticket
        self._check_kerb_ticket()
        tmp = self._get_tmp_workdir()
        self._change_workdir(tmp)
        images = self._get_images()
        self.distgit.dist_git_changes(images, rebase)
        self.logger.info("\nGit location: " + tmp)
        if self.args:
            template = "./rebuild-helper {} git show"
            self.logger.info("You can view changes made by running:")
            self.logger.info(template.format('--base ' + self.base_image + (' --tmp ' + self.tmp_workdir if self.tmp_workdir else "")))
        if self.args:
            self.logger.info("To push and build run: rebuild-helper git push && rebuild-helper build [base/core/s2i] --repo-url link-to-repo-file")

    @needs_distgit
    def merge_future_branches(self):
        """Merges current branch with future branches"""
        # Check for kerberos ticket
        self._check_kerb_ticket()
        tmp = self._get_tmp_workdir()
        self._change_workdir(tmp)
        images = self._get_images()
        self.distgit.merge_future_branches(images)

    @needs_distgit
    def show_git_changes(self, components=None):
        """Shows changes made to tracked files in local downstream repositories

        Args:
            components (list of str, optional): List of components to show changes for
        Walks through all downstream repositories and calls 'git-show' on each of them.
        """
        if not components:
            images = self._get_images()
            components = [i["component"] for i in images]
        tmp = self._get_tmp_workdir()
        self._change_workdir(tmp)
        self.distgit.show_git_changes(tmp, components)

    @needs_dhapi
    def update_dh_description(self):  # TODO: handle login if config changes during a run
        self.pull_upstream()

        imgs = self._get_images()

        for img in imgs:
            #FIXME: Will not work with new config
            name, version, component, branch, url, path, *rest = img

            with open(os.path.join(name.split('-')[0], path, "README.md")) as f:
                desc = "".join(f.readlines())
                self.dhapi.set_repository_full_description(namespace="centos", repo_name=name.replace("rhel", "centos"), full_description=desc)
