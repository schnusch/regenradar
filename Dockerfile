FROM docker.io/selenium/standalone-firefox:latest

COPY --chown=seluser:seluser [".", "/tmp/src"]
RUN ["pip3", "install", "/tmp/src"]
