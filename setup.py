# Always prefer setuptools over distutils
from setuptools import setup, find_packages
# To use a consistent encoding
from codecs import open
from os import path

here = path.abspath(path.dirname(__file__))

# Get the long description from the README file
with open(path.join(here, 'README.md'), encoding='utf-8') as f:
    long_description = f.read()


# Proceed to setup
setup(
    name='qsketch',
    version='0.2',
    description='Tools for Sliced Wasserstein and friends',
    long_description=long_description,
    long_description_content_type='text/markdown',

    url='https://github.com/ANR-kamoulox/qsketch',
    author='Antoine Liutkus',
    author_email='antoine.liutkus@inria.fr',
    packages=find_packages(exclude=['contrib', 'docs', 'tests']),

    keywords='compressed learning sliced wasserstein',
    install_requires=[]
    )
