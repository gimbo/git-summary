#!/usr/bin/env python3

from distutils.core import setup

setup(
    name='git-summary',
    version='1.0',
    description='Summarise a bunch of git repositories in some folder',
    install_requires=[
        'colorama>=0.3.7',
        'gitpython>=2.1.3',
        'sh>=1.10.0',
    ],
    entry_points={
        'console_scripts': [
            'git-summary=git_summary:main',
        ],
    },
)
