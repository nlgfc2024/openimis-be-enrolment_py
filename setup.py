from setuptools import find_packages, setup


setup(
    name="openimis-be-enrolment",
    version="0.1.0",
    packages=find_packages(),
    include_package_data=True,
    license="GNU AGPL v3",
    description="Dedicated openIMIS enrolment orchestration module.",
    install_requires=[
        "django",
        "graphene-django",
        "graphene-django-optimizer",
        "openimis-be-core",
        "openimis-be-individual",
        "openimis-be-social_protection",
        "openimis-be-tasks_management",
    ],
)
