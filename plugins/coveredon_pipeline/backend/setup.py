#!/usr/bin/env python
import os

from setuptools import find_packages, setup

PROJECT_DIR = os.path.dirname(__file__)

setup(
    name="coveredon-pipeline",
    version="1.1.0",
    description="Covered On pipeline triage & stats plugin for Baserow 2.3.3",
    platforms=["linux"],
    package_dir={"": "src"},
    packages=find_packages("src"),
    include_package_data=True,
    install_requires=[],
)