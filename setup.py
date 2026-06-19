#!/usr/bin/env python3
"""
Setup configuration for mono-imager
"""

from setuptools import setup, find_packages
from pathlib import Path

# Read README for long description
readme_file = Path(__file__).parent / "README.md"
long_description = readme_file.read_text() if readme_file.exists() else ""

setup(
    name="mono-imager",
    version="0.1.0",
    description="Automated firmware flashing tool for Mono Gateway Routers and Development Kit",
    long_description=long_description,
    long_description_content_type="text/markdown",
    author="Community Contributors",
    author_email="",
    url="https://github.com/HAHermsen/mono-imager",
    license="MIT",
    packages=find_packages(),
    python_requires=">=3.8",
    install_requires=[
        "pyserial>=3.5",
        "requests>=2.28.0",
    ],
    entry_points={
        "console_scripts": [
            "mono-imager=mono_imager.cli:main",
        ],
    },
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Environment :: Console",
        "Intended Audience :: Developers",
        "Intended Audience :: System Administrators",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Topic :: System :: Hardware",
        "Topic :: System :: Software Distribution",
    ],
    keywords="mono gateway firmware flashing embedded linux uboot",
    project_urls={
        "Documentation": "https://github.com/HAHermsen/mono-imager#readme",
        "Source": "https://github.com/HAHermsen/mono-imager",
        "Tracker": "https://github.com/HAHermsen/mono-imager/issues",
    },
)
