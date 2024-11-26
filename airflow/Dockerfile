# Start from the Apache Airflow image
FROM apache/airflow:2.9.1

# Install OpenJDK 11 from a known working OpenJDK image
FROM openjdk:11-jdk-slim

# Switch to root user to install dependencies
USER root

# Install necessary dependencies for Airflow
RUN apt-get update && \
    apt-get install -y wget gnupg lsb-release && \
    apt-get clean

# Set the JAVA_HOME environment variable
ENV JAVA_HOME=/usr/lib/jvm/java-11-openjdk-amd64

# Verify Java installation
RUN java -version

# Switch back to airflow user
USER airflow
