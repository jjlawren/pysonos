# -*- coding: utf-8 -*-
"""This module contains methods for discovering Sonos devices on the
network."""

from __future__ import unicode_literals

import logging
import socket
import select
from textwrap import dedent
import time
import struct
import ifaddr

from . import config
from .utils import really_utf8

_LOG = logging.getLogger(__name__)

# pylint: disable=too-many-locals, too-many-branches


def discover(timeout=5, include_invisible=False, interface_addr=None):
    """ Discover Sonos zones on the local network.

    Return a set of `SoCo` instances for each zone found.
    Include invisible zones (bridges and slave zones in stereo pairs if
    ``include_invisible`` is `True`. Will block for up to ``timeout`` seconds,
     after which return `None` if no zones found.

    Args:
        timeout (int, optional): block for this many seconds, at most.
            Defaults to 5.
        include_invisible (bool, optional): include invisible zones in the
            return set. Defaults to `False`.
        interface_addr (str or None): Discovery operates by sending UDP
            multicast datagrams. ``interface_addr`` is a string (dotted
            quad) representation of the network interface address to use as
            the source of the datagrams (i.e. it is a value for
            `socket.IP_MULTICAST_IF <socket>`). If `None` or not specified,
            all system interfaces will be tried. Defaults to `None`.
    Returns:
        set: a set of `SoCo` instances, one for each zone found, or else
            `None`.

    """

    def create_socket(interface_addr=None):
        """ A helper function for creating a socket for discover purposes.

        Create and return a socket with appropriate options set for multicast.
        """

        _sock = socket.socket(
            socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        # UPnP v1.0 requires a TTL of 4
        _sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL,
                         struct.pack("B", 4))
        if interface_addr is not None:
            _sock.setsockopt(
                socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                socket.inet_aton(interface_addr))
        return _sock

    # pylint: disable=invalid-name
    PLAYER_SEARCH = dedent("""\
        M-SEARCH * HTTP/1.1
        HOST: 239.255.255.250:1900
        MAN: "ssdp:discover"
        MX: 1
        ST: urn:schemas-upnp-org:device:ZonePlayer:1
        """).encode('utf-8')
    MCAST_GRP = "239.255.255.250"
    MCAST_PORT = 1900

    _sockets = {}
    # Use the specified interface, if any
    if interface_addr is not None:
        try:
            address = socket.inet_aton(interface_addr)
        except socket.error:
            raise ValueError("{0} is not a valid IP address string".format(
                interface_addr))
        _sockets[interface_addr] = create_socket(interface_addr)
        _LOG.info("Sending discovery packets on default interface")
    else:
        # Find the local network addresses using ifaddr.
        addresses = [
            ip.ip
            for adapter in ifaddr.get_adapters()
            for ip in adapter.ips
            if ip.is_IPv4
            if ip.ip != "127.0.0.1"
        ]

        # Create a socket for each unique address found, and one for the
        # default multicast address
        for address in addresses:
            try:
                _sockets[address] = create_socket(address)
            except socket.error as e:
                _LOG.warning("Can't make a discovery socket for %s: %s: %s",
                             address, e.__class__.__name__, e)

    # Note: this is sensitive to clock adjustments. AFAIK there
    # is no monotonic timer available before Python 3.3.
    t0 = time.time()
    while True:
        time_left = timeout - (time.time() - t0)
        if time_left < 0:
            return None

        # Send each second, UDP is unreliable
        for _addr, _sock in _sockets.items():
            try:
                _LOG.info("Sending discovery packet on %s", _addr)
                _sock.sendto(
                    really_utf8(PLAYER_SEARCH), (MCAST_GRP, MCAST_PORT))
            except OSError:
                _LOG.info("Discovery failed on %s", _addr)

        response, _, _ = select.select(
            list(_sockets.values()), [], [], min(1, time_left))

        # Only Zone Players should respond, given the value of ST in the
        # PLAYER_SEARCH message. However, to prevent misbehaved devices
        # on the network disrupting the discovery process, we check that
        # the response contains the "Sonos" string; otherwise we keep
        # waiting for a correct response.
        #
        # Here is a sample response from a real Sonos device (actual numbers
        # have been redacted):
        # HTTP/1.1 200 OK
        # CACHE-CONTROL: max-age = 1800
        # EXT:
        # LOCATION: http://***.***.***.***:1400/xml/device_description.xml
        # SERVER: Linux UPnP/1.0 Sonos/26.1-76230 (ZPS3)
        # ST: urn:schemas-upnp-org:device:ZonePlayer:1
        # USN: uuid:RINCON_B8*************00::urn:schemas-upnp-org:device:
        #                                                     ZonePlayer:1
        # X-RINCON-BOOTSEQ: 3
        # X-RINCON-HOUSEHOLD: Sonos_7O********************R7eU

        if response:
            for _sock in response:
                data, addr = _sock.recvfrom(1024)
                _LOG.debug(
                    'Received discovery response from %s: "%s"', addr, data
                )
                if b"Sonos" in data:
                    # Now we have an IP, we can build a SoCo instance and query
                    # that player for the topology to find the other players.
                    # It is much more efficient to rely upon the Zone
                    # Player's ability to find the others, than to wait for
                    # query responses from them ourselves.
                    zone = config.SOCO_CLASS(addr[0])
                    if include_invisible:
                        return zone.all_zones
                    else:
                        return zone.visible_zones


def any_soco():
    """Return any visible soco device, for when it doesn't matter which.

    Try to obtain an existing instance, or use `discover` if necessary.
    Note that this assumes that the existing instance has not left
    the network.

    Returns:
        SoCo: A `SoCo` instance (or subclass if `config.SOCO_CLASS` is set,
            or `None` if no instances are found
    """

    cls = config.SOCO_CLASS
    # pylint: disable=no-member, protected-access
    try:
        # Try to get the first pre-existing soco instance we know about,
        # as long as it is visible (i.e. not a bridge etc). Otherwise,
        # perform discovery (again, excluding invisibles) and return one of
        # those
        device = next(d for d in cls._instances[cls._class_group].values()
                      if d.is_visible)
    except (KeyError, StopIteration):
        devices = discover()
        return None if devices is None else devices.pop()

    return device


def by_name(name):
    """Return a device by name.

    Args:
        name (str): The name of the device to return.

    Returns:
        :class:`~.SoCo`: The first device encountered among all zone with the
            given player name. If none are found `None` is returned.
    """
    for device in discover():
        if device.player_name == name:
            return device
    return None
