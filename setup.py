from glob import glob
import os
from setuptools import find_packages, setup

package_name = "system_health_tools"

setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", [os.path.join("resource", package_name)]),
        (os.path.join("share", package_name), ["package.xml", "README.md"]),
        (os.path.join("share", package_name, "config"), glob(os.path.join("config", "*.yaml"))),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="Dejan Djordjevic",
    maintainer_email="dejan.djordjevic@coming.rs",
    description="Generic ROS2 tools for extracting and monitoring system health contracts.",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "system_health_monitor = system_health_tools.system_health_monitor:main",
            "system_health_cli = system_health_tools.system_health_cli:main",
            "system_health_rviz = system_health_tools.system_health_rviz:main",
            "extract_expected_system = system_health_tools.extract_expected_system:main",
            "build_system_health_from_runtime = system_health_tools.build_system_health_from_runtime:main",
            "build_health = system_health_tools.build_system_health_from_runtime:main",
        ],
    },
)
