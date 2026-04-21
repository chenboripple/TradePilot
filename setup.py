from setuptools import setup, find_packages

setup(
    name="ripple_tradePilot",
    version="0.1.0",
    description="Wave trading system with shared strategy core",
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    install_requires=[
        "pandas>=2.0",
        "numpy>=1.24",
        "matplotlib>=3.7",
        "fastapi>=0.100",
        "uvicorn>=0.23",
        "tushare>=1.2",
        "akshare>=1.10",
        "pyyaml>=6.0",
        "httpx>=0.24",
        "click>=8.0",
    ],
    entry_points={
        "console_scripts": [
            "tradepilot=ripple_tradePilot.cli:cli",
        ],
    },
    python_requires=">=3.9",
)
