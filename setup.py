#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# The MIT License (MIT)
#
# Copyright (c) 2016-2018 CWT Authors
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#
# Authors: Petr Kubat <pkubat@redhat.com>

import os
import sys

from setuptools import setup, find_packages

VIRTUAL_ENV = hasattr(sys, 'real_prefix')


def get_dir(system_path=None, virtual_path=None):
    """
    Retrieve VIRTUAL_ENV friendly path
    :param system_path: Relative system path
    :param virtual_path: Overrides system_path for virtual_env only
    :return: VIRTUAL_ENV friendly path
    """
    if virtual_path is None:
        virtual_path = system_path
    if VIRTUAL_ENV:
        if virtual_path is None:
            virtual_path = []
        return os.path.join(*virtual_path)
    else:
        if system_path is None:
            system_path = []
    return os.path.join(*(['/'] + system_path))


data_files = {}
paths = ['config']

for path in paths:
    for root, dirs, files in os.walk(path, followlinks=True):
        data_files[
            get_dir(
                ['usr', 'share', 'cwt', root])] = [
            os.path.join(root, f) for f in files]

paths = ['man']

for path in paths:
    for root, dirs, files in os.walk(path, followlinks=True):
        data_files[
            get_dir(
                ['usr', 'share', 'man', 'man1'])] = [
            os.path.join(root, f) for f in files]

setup(
    name='container-workflow-tool',
    version="0.5.0",
    description='A python3 tool to make rebuilding images easier by automating several steps of the process.',
    keywords='tool,containers,images,automate, workflow',
    author='Petr Kubat',
    author_email='pkubat@redhat.com',
    url='https://github.com/sclorg/container-workflow-tool',
    license='MIT',
    packages=find_packages(exclude=['config', 'man', 'test']),
    include_package_data=True,
    data_files=data_files.items(),
    scripts=[],
    entry_points={
        'console_scripts': [
            'cwt = container_workflow_tool.cli:run',
        ]
    },
    setup_requires=[],
    classifiers=[
        'Development Status :: 4 - Beta',
        'Environment :: Console',
        'Intended Audience :: Developers',
        'License :: OSI Approved :: MIT License',
        'Operating System :: POSIX :: Linux',
        'Programming Language :: Python',
        'Topic :: Software Development',
    ],
    install_requires=open('requirements.txt').read().splitlines(),
    zip_safe=True
)
