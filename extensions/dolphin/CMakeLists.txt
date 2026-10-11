# Spola's overlay icons in Dolphin (and every KDE file dialog): a KIO overlay
# icon plugin that asks Spola over D-Bus, like the Python extension of Files.
# It must be built against the KDE Frameworks of the system it runs on, so an
# app cannot install it for the user: see README.md here.
cmake_minimum_required(VERSION 3.20)
project(spola-dolphin-overlay LANGUAGES CXX)

find_package(ECM 6.0 REQUIRED NO_MODULE)
set(CMAKE_MODULE_PATH ${ECM_MODULE_PATH})
include(KDEInstallDirs)
include(KDECMakeSettings)
include(KDECompilerSettings NO_POLICY_SCOPE)

find_package(Qt6 6.5 REQUIRED COMPONENTS Core DBus)
find_package(KF6 6.0 REQUIRED COMPONENTS KIO)

add_library(spolaoverlayplugin MODULE spolaoverlayplugin.cpp)
target_link_libraries(spolaoverlayplugin PRIVATE KF6::KIOWidgets Qt6::DBus)
install(TARGETS spolaoverlayplugin DESTINATION ${KDE_INSTALL_PLUGINDIR}/kf6/overlayicon)
