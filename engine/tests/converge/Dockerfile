# The converge job's test host: Debian 13 with systemd as PID 1, reached by
# Ansible over SSH on port 2222. A CI test host only; Cloudfall itself runs no
# containers (D-10).
FROM debian:trixie

ENV container=docker
ENV DEBIAN_FRONTEND=noninteractive

# What a fresh cloud image has and Ansible needs: systemd, D-Bus (timedatectl),
# sshd, Python for the modules, and sudo for become_user
RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        dbus \
        openssh-server \
        python3 \
        python3-apt \
        sudo \
        systemd \
        systemd-sysv \
    && rm -rf /var/lib/apt/lists/* \
    && printf 'Port 2222\n' > /etc/ssh/sshd_config.d/00-converge-port.conf \
    && install --directory --mode=0700 /root/.ssh \
    && systemctl enable ssh.service \
    && systemctl mask getty@tty1.service console-getty.service

STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
