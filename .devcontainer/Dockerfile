FROM mcr.microsoft.com/devcontainers/python:1-3.12-bookworm

# System libraries needed by Qt 6 / PySide6 (xcb platform plugin, OpenGL, fonts, D-Bus),
# Xvfb for headless pytest-qt runs, xdg-utils for "open file / folder", and
# tools needed to build the AppImage.
# The base image ships a Yarn apt source whose signing key has expired, which makes
# `apt-get update` fail; Yarn isn't needed here, so drop the source.
RUN rm -f /etc/apt/sources.list.d/yarn.list \
    && apt-get update && export DEBIAN_FRONTEND=noninteractive \
    && apt-get install -y --no-install-recommends \
        libgl1 libegl1 libopengl0 libglib2.0-0 libdbus-1-3 \
        libfontconfig1 libfreetype6 fonts-dejavu-core \
        libxkbcommon0 libxkbcommon-x11-0 \
        libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 \
        libxcb-randr0 libxcb-render-util0 libxcb-shape0 libxcb-xinerama0 \
        libxcb-xfixes0 libxcb-xkb1 \
        libwayland-client0 libwayland-cursor0 libwayland-egl1 \
        xvfb xauth x11-utils xdg-utils shared-mime-info \
        desktop-file-utils libfuse2 file wget \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
