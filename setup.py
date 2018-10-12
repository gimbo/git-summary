#!/usr/bin/env python3

from distutils.core import setup

setup(
    name='git-summary',
    version='1.0',
    description='Summarise a bunch of git repositories in some folder',
    author='Andy Gimblett',
    author_email='andy@barefootcode.com',
    classifiers=[
        'Development Status :: 5 - Production/Stable',
        'Environment :: Console',
        'Intended Audience :: Developers',
        'Intended Audience :: System Administrators',
        'License :: OSI Approved :: MIT License',
        'Programming Language :: Python :: 3 :: Only',
        'Topic :: Software Development :: Version Control :: Git',
        'Topic :: Utilities ',
    ],
    project_urls={
        'Source': 'https://github.com/gimbo/git-summary',
    },
    keywords='command-line git tool',
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
