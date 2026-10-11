# The test bench of tools/file_managers_smoke.sh: Fedora with the file managers
# Spola extends (Files, Nemo, Caja, Dolphin, Thunar) and their Python bindings, a
# virtual X screen, ImageMagick for the screenshots, and the KDE Frameworks
# headers to build the Dolphin plugin.
#   podman build -t localhost/spola-fmtest -f tools/file-managers.Containerfile tools
FROM registry.fedoraproject.org/fedora:44
RUN dnf -y install --setopt=install_weak_deps=False \
      nautilus nautilus-python nemo nemo-python xapps xapp-symbolic-icons caja python3-caja Thunar xfconf dolphin \
      xorg-x11-server-Xvfb xdotool ImageMagick dbus-daemon dbus-x11 \
      python3-gobject gtk4 libadwaita libsecret rclone glib2 gettext \
      cmake extra-cmake-modules gcc-c++ kf6-kio-devel kf6-kcoreaddons-devel qt6-qtbase-devel \
      adwaita-icon-theme breeze-icon-theme hicolor-icon-theme google-noto-sans-fonts \
    && dnf clean all \
    && mkdir -p /home/me && chmod 777 /home/me
