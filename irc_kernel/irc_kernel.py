import argparse
import asyncio
import datetime
import json
import pathlib
import random
import sys
import uuid


class Config:
    def __contains__(self, item):
        return item in self.data

    def __getitem__(self, item):
        return self.data[item]

    def __init__(self, path):
        self.path = path
        self.data = {}
        if self.path.exists():
            with self.path.open() as f:
                self.data = json.load(f)

    def __setitem__(self, key, value):
        self.data[key] = value
        self._flush()

    def _flush(self):
        with self.path.open('w') as f:
            json.dump(self.data, f, indent=2, sort_keys=True)

    def get(self, key, default=None):
        return self.data.get(key, default)

    def keys(self):
        return self.data.keys()

    def remove(self, key):
        if key in self.data:
            del self.data[key]
            self._flush()

    def set(self, key, value):
        self[key] = value
        self._flush()


class NewlineDelimitedProtocol(asyncio.Protocol):

    _buf = b''

    def data_received(self, data):
        self._buf = self._buf + data
        lines = self._buf.split(b'\n')
        self._buf = lines.pop()
        for line in lines:
            self.line_received(line.strip())

    def line_received(self, line):
        raise NotImplementedError


class KernelControl(NewlineDelimitedProtocol):

    def __call__(self):
        return self

    def __init__(self, config, verbose=False):
        self.config = config
        self.verbose = verbose
        self._t = None
        self.subscribed = False
        self.clients = {}
        for name, net in self.config['networks'].items():
            client = IRCClient(name, net, self)
            loop = asyncio.get_event_loop()
            c = loop.create_connection(client, net['host'], net['port'])
            asyncio.ensure_future(c)
            self.clients[name] = client

    def connection_made(self, transport):
        peername = transport.get_extra_info('peername')
        self.log('** New control connection from {}'.format(peername))
        self._t = transport

    def connection_lost(self, exc):
        self.log('** Control connection closed')

    def control_disconnect(self):
        self._t.close()

    def dispatch_method(self, msg):
        if msg.get('jsonrpc') != '2.0':
            self.control_disconnect()
            return

        if 'method' not in msg:
            self.control_disconnect()
            return

        params = msg.get('params')
        if not isinstance(params, dict):
            self.control_disconnect()
            return

        if params.get('secret') == self.config['control']['secret']:
            method = msg.get('method')
            if method == 'control.disconnect':
                self.control_disconnect()
            if method == 'network.add':
                return self.network_add(msg)
            if method == 'network.delete':
                return self.network_delete(msg)
            if method == 'network.get':
                return self.network_get(msg)
            if method == 'network.send':
                return self.network_send(msg)
            if method == 'stream.start':
                return self.stream_start(msg)
            if method == 'stream.stop':
                return self.stream_stop(msg)

        self.control_disconnect()
        return

    def irc_handler(self, client, msg):
        self.out({'jsonrpc': '2.0', 'method': 'handler',
                  'params': {'network': client.name, 'message': msg}})

    def line_received(self, line):
        try:
            line = line.decode()
        except UnicodeDecodeError:
            self.control_disconnect()
            return

        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            self.control_disconnect()
            return

        if isinstance(msg, dict):
            self.out(self.dispatch_method(msg))
            return

        if isinstance(msg, list):
            self.out([self.dispatch_method(m) for m in msg])
            return

        self.control_disconnect()
        return

    @staticmethod
    def log(m):
        log('[control] {}'.format(m))

    def network_add(self, msg):
        params = msg.get('params')
        host = params.get('host')
        if host is None:
            self.control_disconnect()
            return
        name = params.get('name')
        nick = params.get('nick')
        port = params.get('port', 6667)
        realname = params.get('realname')
        user = params.get('user')

        net = dict(host=host, nick=nick, port=port, realname=realname,
                   user=user)
        net['channels'] = []
        saved_networks = self.config['networks']
        saved_networks[name] = net
        self.config['networks'] = saved_networks

        client = IRCClient(name, net, self)
        if self.subscribed:
            client.subscribe(self.irc_handler)
        loop = asyncio.get_event_loop()
        c = loop.create_connection(client, host, port)
        asyncio.ensure_future(c)
        self.clients[name] = client

        return {'result': 'success', 'id': msg.get('id'), 'jsonrpc': '2.0'}

    def network_delete(self, msg):
        params = msg.get('params')
        name = params.get('name')
        client = self.clients.get(name)
        if client is None:
            message = 'network.delete: unknown network {!r}'.format(name)
            return {'jsonrpc': '2.0', 'id': msg.get('id'),
                    'error': {'code': -32002, 'message': message}}
        client.disconnect()
        del self.clients[name]
        saved_networks = self.config['networks']
        del saved_networks[name]
        self.config['networks'] = saved_networks
        return {'result': 'success', 'id': msg.get('id'), 'jsonrpc': '2.0'}

    def network_get(self, msg):
        networks = self.config['networks']
        return {'result': networks, 'id': msg.get('id'), 'jsonrpc': '2.0'}

    def network_send(self, msg):
        params = msg.get('params')
        name = params.get('name')
        client = self.clients.get(name)
        if client is None:
            message = 'network.send: unknown network {!r}'.format(name)
            return {'jsonrpc': '2.0', 'id': msg.get('id'),
                    'error': {'code': -32001, 'message': message}}
        message = params.get('message')
        if message.lower().startswith('join '):
            chan_list = message.split()[1].split(',')
            saved_networks = self.config['networks']
            saved_channels = set(saved_networks[name]['channels'])
            saved_channels.update(set(chan_list))
            saved_networks[name]['channels'] = list(saved_channels)
            self.config['networks'] = saved_networks
        elif message.lower().startswith('nick '):
            new_nick = message.split()[1]
            saved_networks = self.config['networks']
            saved_networks[name]['nick'] = new_nick
            self.config['networks'] = saved_networks
        elif message.lower().startswith('part '):
            chan_list = message.split()[1].split(',')
            saved_networks = self.config['networks']
            saved_channels = set(saved_networks[name]['channels'])
            saved_channels.difference_update(set(chan_list))
            saved_networks[name]['channels'] = list(saved_channels)
            self.config['networks'] = saved_networks
        client.out(message)
        return {'result': 'success', 'id': msg.get('id'), 'jsonrpc': '2.0'}

    def out(self, line):
        line = json.dumps(line)
        self._t.write('{}\n'.format(line).encode())

    def stream_start(self, msg):
        if self.verbose:
            self.log('** Controller requested to start the stream')
        self.subscribed = True
        for c in self.clients.values():
            c.subscribe(self.irc_handler)
        return {'result': 'success', 'id': msg.get('id'), 'jsonrpc': '2.0'}

    def stream_stop(self, msg):
        self.subscribed = False
        for c in self.clients.values():
            c.unsubscribe(self.irc_handler)
        return {'result': 'success', 'id': msg.get('id'), 'jsonrpc': '2.0'}


class IRCClient(NewlineDelimitedProtocol):

    def __init__(self, name, net, controller: KernelControl):
        self._loop = asyncio.get_event_loop()
        self._t = None
        self._subscribers = set()

        self.name = name
        self.net = net
        self.controller = controller

    def __call__(self):
        return self

    def line_received(self, line):
        if self.controller.verbose:
            self.log('<= {!r}'.format(line))
        line = self.decode(line)
        for handler in self._subscribers:
            handler(self, line)
        tokens = line.split()
        if len(tokens) > 0 and tokens[0] == 'PING':
            self.out('PONG ' + tokens[1])
        elif len(tokens) > 1:
            if tokens[1] == '376':
                self.join_saved_channels()

    def connection_lost(self, exc):
        self.log('** Connection lost')

    def connection_made(self, transport):
        peer = transport.get_extra_info('peername')
        self._t = transport
        if self.controller.verbose:
            self.log('** Connection made to {}'.format(peer))
        self.out('NICK ' + self.net['nick'])
        self.out('USER {} {} x :{}'.format(self.net['user'], self.net['host'],
                                           self.net['realname']))

    def decode(self, m):
        try:
            return m.decode()
        except UnicodeDecodeError:
            self.log('** Failed decode using utf-8.')
            pass
        try:
            return m.decode('iso-8859-1')
        except:
            self.log('** Failed decode using iso-8859-1.')
            self.log(m)
            raise

    def disconnect(self):
        self._t.close()

    def join_saved_channels(self):
        for channel in self.net['channels']:
            self.out('JOIN {}'.format(channel))

    def log(self, m):
        log('[{}] {}'.format(self.name, m))

    def out(self, m):
        if m:
            m = '{}\r\n'.format(m).encode()
            if self.controller.verbose:
                self.log('=> {!r}'.format(m))
            self._t.write(m)

    def subscribe(self, handler):
        self._subscribers.add(handler)

    def unsubscribe(self, handler):
        self._subscribers.discard(handler)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('-v', '--verbose', action='store_true')
    return parser.parse_args()


def generate_config(path: pathlib.Path):
    secret = str(uuid.uuid4())
    nick = 'i' + secret[:7]
    default_config = {
        'control': {
            'secret': secret,
            'host': '0.0.0.0',
            'port': random.randint(49152, 65535)
        },
        'networks': {
            'freenode': {
                'host': 'chat.freenode.net',
                'port': 6667,
                'nick': nick,
                'realname': nick,
                'user': nick,
                'channels': ['#{}'.format(nick)]
            }
        }
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w') as f:
        json.dump(default_config, f, indent=2, sort_keys=True)


def log(m):
    t = datetime.datetime.utcnow()
    print('{} {}'.format(t, m))
    sys.stdout.flush()


def main():
    log('** Starting up')
    args = parse_args()
    if args.verbose:
        log('** Verbose logging is turned on')

    config_path = pathlib.Path.home() / '.config/irc_kernel/config.json'
    if config_path.exists():
        try:
            c = Config(config_path)
        except json.JSONDecodeError:
            log('** The config file is invalid')
            sys.exit()
    else:
        log('** No config file found')
        generate_config(config_path)
        log('** I generated a new config file at {}'.format(config_path))
        log('** Edit it and try again')
        sys.exit()

    kc = KernelControl(c, args.verbose)
    loop = asyncio.get_event_loop()
    host = c['control']['host']
    port = c['control']['port']
    k = loop.create_server(kc, host, port)
    log('** Listening for control connections on {}:{}'.format(host, port))
    loop.run_until_complete(k)

    try:
        loop.run_forever()
    except KeyboardInterrupt:
        log('** Shutting down')
        loop.stop()

if __name__ == '__main__':
    main()
