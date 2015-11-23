from setuptools import find_packages, setup

setup(
    name='irc-kernel',
    version='0.1',
    packages=find_packages(),
    entry_points={
        'console_scripts': [
            'irc_kernel = irc_kernel.irc_kernel:main'
        ]
    }
)
