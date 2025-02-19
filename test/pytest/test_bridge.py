import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Dict, Iterable

import pytest

from cockpit._vendor.systemd_ctypes import bus
from cockpit.bridge import Bridge
from cockpit.channels import CHANNEL_TYPES

from .mocktransport import MOCK_HOSTNAME, MockTransport


class test_iface(bus.Object):
    sig = bus.Interface.Signal('s')
    prop = bus.Interface.Property('s', value='none')

    @bus.Interface.Method('s')
    def get_prop(self) -> str:
        return self.prop


@pytest.fixture
def bridge(event_loop) -> Bridge:
    async def get_bridge() -> Bridge:
        bridge = Bridge(argparse.Namespace(privileged=False, beipack=False))
        asyncio.set_event_loop(None)
        # We use this for assertions
        bridge.superuser_bridges = list(bridge.superuser_rule.bridges)  # type: ignore[attr-defined]
        return bridge

    return event_loop.run_until_complete(get_bridge())


def add_pseudo(bridge: Bridge) -> None:
    bridge.more_superuser_bridges = [*bridge.superuser_bridges, 'pseudo']  # type: ignore[attr-defined]

    # Add pseudo to the existing set of superuser rules
    assert bridge.packages is not None
    configs = bridge.packages.get_bridge_configs()
    configs.append({
        'label': 'pseudo',
        'spawn': [
            sys.executable, os.path.abspath(f'{__file__}/../pseudo.py'),
            sys.executable, '-m', 'cockpit.bridge', '--privileged'
        ],
        'environ': [
            f'PYTHONPATH={":".join(sys.path)}'
        ],
        'privileged': True
    })
    bridge.superuser_rule.set_configs(configs)


@pytest.fixture
def no_init_transport(event_loop: asyncio.AbstractEventLoop,
                      bridge: Bridge) -> Iterable[MockTransport]:
    async def get_transport() -> MockTransport:
        return MockTransport(bridge)
    transport = event_loop.run_until_complete(get_transport())
    try:
        yield transport
    finally:
        event_loop.run_until_complete(transport.stop())


@pytest.fixture
def transport(event_loop: asyncio.AbstractEventLoop,
              no_init_transport: MockTransport) -> MockTransport:
    event_loop.run_until_complete(no_init_transport.assert_msg('', command='init'))
    no_init_transport.send_init()
    return no_init_transport


@pytest.mark.asyncio
async def test_echo(transport):
    echo = await transport.check_open('echo')

    transport.send_data(echo, b'foo')
    await transport.assert_data(echo, b'foo')

    transport.send_ping(channel=echo)
    await transport.assert_msg('', command='pong', channel=echo)

    transport.send_done(echo)
    await transport.assert_msg('', command='done', channel=echo)
    await transport.assert_msg('', command='close', channel=echo)


@pytest.mark.asyncio
async def test_host(transport):
    # try to open a null channel, explicitly naming our host
    await transport.check_open('null', host=MOCK_HOSTNAME)

    # try to open a null channel, no host
    await transport.check_open('null')

    # try to open a null channel, a different host which is sure to fail DNS
    await transport.check_open('null', host='¡invalid!', problem='no-host')

    # make sure host check happens before superuser
    # ie: requesting superuser=True on another host should fail because we
    # can't contact the other host ('no-host'), rather than trying to first
    # go to our superuser bridge ('access-denied')
    await transport.check_open('null', host='¡invalid!', superuser=True, problem='no-host')

    # but make sure superuser is indeed failing as we expect, on our host
    await transport.check_open('null', host=MOCK_HOSTNAME, superuser=True, problem='access-denied')


@pytest.mark.asyncio
async def test_dbus_call_internal(bridge, transport):
    my_object = test_iface()
    bridge.internal_bus.export('/foo', my_object)
    assert my_object._dbus_bus == bridge.internal_bus.server
    assert my_object._dbus_path == '/foo'

    values, = await transport.check_bus_call('/foo', 'org.freedesktop.DBus.Properties',
                                             'GetAll', ["test.iface"])
    assert values == {'Prop': {'t': 's', 'v': 'none'}}

    result, = await transport.check_bus_call('/foo', 'test.iface', 'GetProp', [])
    assert result == 'none'


@pytest.mark.asyncio
async def test_dbus_watch(bridge, transport):
    my_object = test_iface()
    bridge.internal_bus.export('/foo', my_object)
    assert my_object._dbus_bus == bridge.internal_bus.server
    assert my_object._dbus_path == '/foo'

    # Add a watch
    internal = await transport.ensure_internal_bus()

    transport.send_json(internal, watch={'path': '/foo', 'interface': 'test.iface'}, id='4')
    meta = await transport.next_msg(internal)
    assert meta['meta']['test.iface'] == {
        'methods': {'GetProp': {'in': [], 'out': ['s']}},
        'properties': {'Prop': {'flags': 'r', 'type': 's'}},
        'signals': {'Sig': {'in': ['s']}}
    }
    notify = await transport.next_msg(internal)
    assert notify['notify']['/foo'] == {'test.iface': {'Prop': 'none'}}
    reply = await transport.next_msg(internal)
    assert reply == {'id': '4', 'reply': []}

    # Change a property
    my_object.prop = 'xyz'
    notify = await transport.next_msg(internal)
    assert 'notify' in notify
    assert notify['notify']['/foo'] == {'test.iface': {'Prop': 'xyz'}}


async def verify_root_bridge_not_running(bridge, transport):
    assert bridge.superuser_rule.peer is None
    await transport.assert_bus_props('/superuser', 'cockpit.Superuser',
                                     {'Bridges': bridge.more_superuser_bridges, 'Current': 'none'})
    null = await transport.check_open('null', superuser=True, problem='access-denied')
    assert null not in bridge.open_channels


@pytest.mark.asyncio
async def verify_root_bridge_running(bridge, transport):
    await transport.assert_bus_props('/superuser', 'cockpit.Superuser',
                                     {'Bridges': bridge.more_superuser_bridges, 'Current': 'pseudo'})
    assert bridge.superuser_rule.peer is not None

    # try to open dbus on the root bridge
    root_dbus = await transport.check_open('dbus-json3', bus='internal', superuser=True)

    # verify that the bridge thinks that it's the root bridge
    await transport.assert_bus_props('/superuser', 'cockpit.Superuser',
                                     {'Bridges': [], 'Current': 'root'}, bus=root_dbus)

    # close up
    await transport.check_close(channel=root_dbus)


@pytest.mark.asyncio
async def test_superuser_dbus(bridge, transport):
    add_pseudo(bridge)
    await verify_root_bridge_not_running(bridge, transport)

    # start the superuser bridge -- no password, so it should work straight away
    () = await transport.check_bus_call('/superuser', 'cockpit.Superuser', 'Start', ['pseudo'])

    await verify_root_bridge_running(bridge, transport)

    # open a channel on the root bridge
    root_null = await transport.check_open('null', superuser=True)

    # stop the bridge
    stop = transport.send_bus_call(transport.internal_bus, '/superuser',
                                   'cockpit.Superuser', 'Stop', [])

    # that should have implicitly closed the open channel
    await transport.assert_msg('', command='close', channel=root_null)
    assert root_null not in bridge.open_channels

    # The Stop method call is done now
    await transport.assert_msg(transport.internal_bus, reply=[[]], id=stop)


def format_methods(methods: Dict[str, str]):
    return {name: {'t': 'a{sv}', 'v': {'label': {'t': 's', 'v': label}}} for name, label in methods.items()}


@pytest.mark.asyncio
async def test_superuser_dbus_pw(bridge, transport, monkeypatch):
    monkeypatch.setenv('PSEUDO_PASSWORD', 'p4ssw0rd')
    add_pseudo(bridge)
    await verify_root_bridge_not_running(bridge, transport)

    # watch for signals
    await transport.add_bus_match('/superuser', 'cockpit.Superuser')
    await transport.watch_bus('/superuser', 'cockpit.Superuser',
                              {
                                  'Bridges': bridge.more_superuser_bridges,
                                  'Current': 'none',
                                  'Methods': format_methods({'pseudo': 'pseudo'}),
                              })

    # start the bridge.  with a password this is more complicated
    start = transport.send_bus_call(transport.internal_bus, '/superuser',
                                    'cockpit.Superuser', 'Start', ['pseudo'])
    # first, init state
    await transport.assert_bus_notify('/superuser', 'cockpit.Superuser', {'Current': 'init'})
    # then, we'll be asked for a password
    await transport.assert_bus_signal('/superuser', 'cockpit.Superuser', 'Prompt',
                                      ['', 'can haz pw?', '', False, ''])
    # give it
    await transport.check_bus_call('/superuser', 'cockpit.Superuser', 'Answer', ['p4ssw0rd'])
    # and now the bridge should be running
    await transport.assert_bus_notify('/superuser', 'cockpit.Superuser', {'Current': 'pseudo'})

    # Start call is now done
    await transport.assert_bus_reply(start, [])

    # double-check
    await verify_root_bridge_running(bridge, transport)


@pytest.mark.asyncio
async def test_superuser_dbus_wrong_pw(bridge, transport, monkeypatch):
    monkeypatch.setenv('PSEUDO_PASSWORD', 'p4ssw0rd')
    add_pseudo(bridge)
    await verify_root_bridge_not_running(bridge, transport)

    # watch for signals
    await transport.add_bus_match('/superuser', 'cockpit.Superuser')
    await transport.watch_bus('/superuser', 'cockpit.Superuser',
                              {
                                  'Bridges': bridge.more_superuser_bridges,
                                  'Current': 'none',
                                  'Methods': format_methods({'pseudo': 'pseudo'}),
                              })

    # start the bridge.  with a password this is more complicated
    start = transport.send_bus_call(transport.internal_bus, '/superuser',
                                    'cockpit.Superuser', 'Start', ['pseudo'])
    # first, init state
    await transport.assert_bus_notify('/superuser', 'cockpit.Superuser', {'Current': 'init'})
    # then, we'll be asked for a password
    await transport.assert_bus_signal('/superuser', 'cockpit.Superuser', 'Prompt',
                                      ['', 'can haz pw?', '', False, ''])
    # give it
    await transport.check_bus_call('/superuser', 'cockpit.Superuser',
                                   'Answer', ['p5ssw0rd'])  # wrong password
    # pseudo fails after the first wrong attempt
    await transport.assert_bus_notify('/superuser', 'cockpit.Superuser', {'Current': 'none'})

    # Start call is now done and returned failure
    await transport.assert_bus_error(start, 'cockpit.Superuser.Error', 'pseudo says: Bad password')

    # double-check
    await verify_root_bridge_not_running(bridge, transport)


@pytest.mark.asyncio
async def test_superuser_init(bridge, no_init_transport):
    add_pseudo(bridge)
    await no_init_transport.assert_msg('', command='init')
    no_init_transport.send_init(superuser={"id": "pseudo"})
    transport = no_init_transport

    # this should work right away without auth
    await transport.assert_msg('', command='superuser-init-done')

    await verify_root_bridge_running(bridge, transport)


@pytest.mark.asyncio
async def test_superuser_init_pw(bridge, no_init_transport, monkeypatch):
    monkeypatch.setenv('PSEUDO_PASSWORD', 'p4ssw0rd')
    add_pseudo(bridge)
    await no_init_transport.assert_msg('', command='init')
    no_init_transport.send_init(superuser={"id": "pseudo"})
    transport = no_init_transport

    msg = await transport.assert_msg('', command='authorize')
    transport.send_json('', command='authorize', cookie=msg['cookie'], response='p4ssw0rd')

    # that should have worked
    await transport.assert_msg('', command='superuser-init-done')

    await verify_root_bridge_running(bridge, transport)


@pytest.mark.asyncio
async def test_no_login_messages(transport):
    await transport.check_bus_call('/LoginMessages', 'cockpit.LoginMessages', 'Get', [], ["{}"])
    await transport.check_bus_call('/LoginMessages', 'cockpit.LoginMessages', 'Dismiss', [], [])
    await transport.check_bus_call('/LoginMessages', 'cockpit.LoginMessages', 'Get', [], ["{}"])


@pytest.fixture
def login_messages_envvar(monkeypatch):
    if sys.version_info < (3, 8):
        pytest.skip("os.memfd_create new in 3.8")
    fd = os.memfd_create('login messages')
    os.write(fd, b"msg")
    # this is questionable (since it relies on ordering of fixtures), but it works
    monkeypatch.setenv('COCKPIT_LOGIN_MESSAGES_MEMFD', str(fd))
    return None


@pytest.mark.asyncio
async def test_login_messages(login_messages_envvar, transport):
    del login_messages_envvar

    await transport.check_bus_call('/LoginMessages', 'cockpit.LoginMessages', 'Get', [], ["msg"])
    # repeated read should get the messages again
    await transport.check_bus_call('/LoginMessages', 'cockpit.LoginMessages', 'Get', [], ["msg"])
    # ...but not after they were dismissed
    await transport.check_bus_call('/LoginMessages', 'cockpit.LoginMessages', 'Dismiss', [], [])
    await transport.check_bus_call('/LoginMessages', 'cockpit.LoginMessages', 'Get', [], ["{}"])
    # idempotency
    await transport.check_bus_call('/LoginMessages', 'cockpit.LoginMessages', 'Dismiss', [], [])
    await transport.check_bus_call('/LoginMessages', 'cockpit.LoginMessages', 'Get', [], ["{}"])


@pytest.mark.asyncio
async def test_freeze(bridge, transport):
    koelle = await transport.check_open('echo')
    malle = await transport.check_open('echo')

    # send a bunch of data to frozen koelle
    bridge.open_channels[koelle].freeze_endpoint()
    transport.send_data(koelle, b'x1')
    transport.send_data(koelle, b'x2')
    transport.send_data(koelle, b'x3')
    transport.send_done(koelle)

    # malle never freezes
    transport.send_data(malle, b'yy')
    transport.send_done(malle)

    # unfreeze koelle
    bridge.open_channels[koelle].thaw_endpoint()

    # malle should have sent its messages first
    await transport.assert_data(malle, b'yy')
    await transport.assert_msg('', command='done', channel=malle)
    await transport.assert_msg('', command='close', channel=malle)

    # the data from koelle should still be in the right order, though
    await transport.assert_data(koelle, b'x1')
    await transport.assert_data(koelle, b'x2')
    await transport.assert_data(koelle, b'x3')
    await transport.assert_msg('', command='done', channel=koelle)
    await transport.assert_msg('', command='close', channel=koelle)


@pytest.mark.asyncio
async def test_internal_metrics(transport):
    metrics = [
        {"name": "cpu.core.user", "derive": "rate"},
        {"name": "memory.used"},
    ]
    interval = 100
    source = 'internal'

    await transport.check_open('metrics1', source=source, interval=interval, metrics=metrics)
    _, data = await transport.next_frame()
    # first message is always the meta message
    meta = json.loads(data)
    assert isinstance(meta['timestamp'], float)
    assert meta['interval'] == interval
    assert meta['source'] == source
    assert isinstance(meta['metrics'], list)
    instances = len([m['instances'] for m in meta['metrics'] if m['name'] == 'cpu.core.user'][0])

    # actual data
    _, data = await transport.next_frame()
    data = json.loads(data)
    # cpu.core.user instances should be the same as meta sent instances
    assert instances == len(data[0][0])
    # all instances should be False, as this is a rate
    assert not all(d for d in data[0][0])
    # memory.used should be an integer
    assert isinstance(data[0][1], int)


@pytest.mark.asyncio
async def test_fsread1_errors(transport):
    await transport.check_open('fsread1', path='/etc/shadow', problem='access-denied')
    await transport.check_open('fsread1', path='/', problem='internal-error',
                               reply_keys={'message': "[Errno 21] Is a directory: '/'"})


@pytest.mark.asyncio
async def test_fslist1_no_watch(transport):
    tempdir = tempfile.TemporaryDirectory()
    dir_path = Path(tempdir.name)

    # empty
    ch = transport.send_open('fslist1', path=str(dir_path), watch=False)
    await transport.assert_msg('', command='done', channel=ch)
    await transport.check_close(channel=ch)

    # create a file and a directory in some_dir
    Path(dir_path, 'somefile').touch()
    Path(dir_path, 'somedir').mkdir()

    ch = transport.send_open('fslist1', path=str(dir_path), watch=False)
    # don't assume any ordering
    msg1 = await transport.next_msg(ch)
    msg2 = await transport.next_msg(ch)
    if msg1['type'] == 'file':
        msg1, msg2 = msg2, msg1
    assert msg1 == {'event': 'present', 'path': 'somedir', 'type': 'directory'}
    assert msg2 == {'event': 'present', 'path': 'somefile', 'type': 'file'}

    await transport.assert_msg('', command='done', channel=ch)
    await transport.check_close(channel=ch)


@pytest.mark.asyncio
async def test_fslist1_notexist(transport):
    await transport.check_open(
        'fslist1', path='/nonexisting', watch=False,
        problem='not-found',
        reply_keys={'message': "[Errno 2] No such file or directory: '/nonexisting'"})


@pytest.mark.asyncio
@pytest.mark.parametrize('channeltype', CHANNEL_TYPES)
async def test_channel(channeltype, tmp_path):
    bridge = Bridge(argparse.Namespace(privileged=False, beipack=False))
    transport = MockTransport(bridge)
    await transport.assert_msg('', command='init')
    transport.send_init()

    payload = channeltype.payload
    args = dict(channeltype.restrictions)

    async def serve_page(reader, writer):
        while True:
            line = await reader.readline()
            if line.strip():
                print('HTTP Request line', line)
            else:
                break

        print('Sending HTTP reply')
        writer.write(b'HTTP/1.1 200 OK\r\n\r\nA document\r\n')
        await writer.drain()
        writer.close()

    srv = str(tmp_path / 'sock')
    await asyncio.start_unix_server(serve_page, srv)

    if payload == 'fslist1':
        args = {'path': '/', 'watch': False}
    elif payload == 'fsread1':
        args = {'path': '/etc/passwd'}
    elif payload == 'fsreplace1':
        args = {'path': 'tmpfile'}
    elif payload == 'fswatch1':
        args = {'path': '/etc'}
    elif payload == 'http-stream1':
        args = {'internal': 'packages', 'method': 'GET', 'path': '/manifests.js',
                'headers': {'X-Forwarded-Proto': 'http', 'X-Forwarded-Host': 'localhost'}}
    elif payload == 'http-stream2':
        args = {'method': 'GET', 'path': '/bzzt', 'unix': srv}
    elif payload == 'stream':
        if 'spawn' in args:
            args = {'spawn': ['cat']}
        else:
            args = {'unix': srv}
    elif payload == 'metrics1':
        args['metrics'] = [{'name': 'memory.free'}]
    elif payload == 'dbus-json3':
        if not os.path.exists('/run/dbus/system_bus_socket'):
            pytest.skip('no dbus')
    else:
        args = {}

    print('sending open', payload, args)
    ch = transport.send_open(payload, **args)
    saw_data = False

    while True:
        channel, msg = await transport.next_frame()
        print(channel, msg)
        if channel == '':
            control = json.loads(msg)
            assert control['channel'] == ch
            command = control['command']
            if command == 'done':
                saw_data = True
            elif command == 'ready':
                # If we get ready, it's our turn to send data first.
                # Hopefully we didn't receive any before.
                assert isinstance(bridge.open_channels[ch], channeltype)
                assert not saw_data
                break
            elif command == 'close':
                # If we got an immediate close message then it should be
                # because the channel sent data and finished, without error.
                assert 'problem' not in control
                assert saw_data
                return
            else:
                pytest.fail('unexpected event', (payload, args, control))
        else:
            saw_data = True

    # If we're here, it's our turn to talk.  Say nothing.
    print('sending done')
    transport.send_done(ch)

    if payload in ['dbus-json3', 'fswatch1', 'null']:
        transport.send_close(ch)

    while True:
        channel, msg = await transport.next_frame()
        print(channel, msg)
        if channel == '':
            control = json.loads(msg)
            command = control['command']
            if command == 'done':
                continue
            elif command == 'close':
                assert 'problem' not in control
                return
