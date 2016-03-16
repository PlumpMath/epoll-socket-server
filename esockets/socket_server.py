#!/bin/env python3
import selectors
import socket
import loopfunction
import logging
import maxthreads
from threading import Lock


class ConnectionBroken(Exception):
    pass


class Client:
    def __init__(self, sock, address):
        self.socket = sock
        self.address = address
        self.send_lock = Lock()
        self.closed = False
        self.accepted = False
        self.close_handled = False

    def fileno(self):
        return self.socket.fileno()

    def close(self):
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except socket.error:
            pass
        self.socket.close()
        self.closed = True

    def send(self, bytes, timeout=-1):
        total_sent = 0
        msg_len = len(bytes)
        # print('Lock acquired')
        if self.send_lock.acquire(timeout=timeout):
            try:
                while total_sent < msg_len:
                    sent = self.socket.send(bytes[total_sent:])
                    if sent == 0:
                        raise RuntimeError('Socket connection broken on send')
                    total_sent = total_sent + sent
            finally:
                self.send_lock.release()

        return total_sent

    def recv(self, size, fixed=True):
        if fixed:
            chunks = []
            bytes_recd = 0
            while bytes_recd < size:
                chunk = self.socket.recv(min(size - bytes_recd, 2048))
                if chunk == b'':
                    raise RuntimeError("Socket connection broken on recv")
                chunks.append(chunk)
                bytes_recd += len(chunk)
            return b''.join(chunks)

        else:
            data = self.socket.recv(size)
            if data == b'':
                raise RuntimeError("Socket connection broken on recv")
            return data


class Log:
    INDENTATION = 4

    def __init__(self, *args_):
        self.do = {'errors': False,
                   'enter': False,
                   'exit': False,
                   'args': False}

        for i in args_:
            if i not in self.do and i != 'all':
                print('ERROR:' + i)
                raise ValueError('{} is not a valid variable'.format(i))

        for i in self.do.keys():
            if i in args_ or 'all' in args_:
                self.do[i] = True

    def __call__(self, f):
        def wrapped_f(*args, **kwargs):
            if self.do['enter']:
                logging.debug(self._indent_string(
                             'function {} called with\n'.format(f.__name__) +
                             'args={}\n'.format(args) +
                             'kwargs={}'.format(kwargs), self.INDENTATION))
            try:
                f(*args, **kwargs)
            except:
                if self.do['errors']:
                    logging.error(self._indent_string(
                                  'function {} was called with\n'.format(f.__name__) +
                                  'args={}\n'.format(args) +
                                  'kwargs={}\n'.format(kwargs) +
                                  'and exited with error:\n' +
                                  '-'*50 + '\n' +
                                  logging.traceback.format_exc() +
                                  '-'*50 + '\n', self.INDENTATION))
                raise
            else:
                if self.do['exit']:
                    logging.debug('function {} exited normally'.format(f.__name__))
        return wrapped_f

    @staticmethod
    def _indent_string(string, indentation):
        return (' '*indentation).join(string.splitlines(True))


class SocketServer:

    @Log('errors')
    def __init__(self,
                 port=1234,
                 host=socket.gethostbyname(socket.gethostname()),
                 queue_size=1000,
                 block_time=2,
                 selector=selectors.EpollSelector,
                 handle_readable=lambda client: True,
                 handle_incoming=lambda client: True,
                 handle_closed=lambda client, reason: True,
                 max_subthreads=-1):

        self.port = port
        self.host = host
        self.queue_size = queue_size
        self.block_time = block_time
        self.selector = selector
        self.handle_readable = handle_readable
        self.handle_incoming = handle_incoming
        self.handle_closed = handle_closed

        self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_socket.setblocking(False)

        self._accept_selector = selector()
        self._recv_selector = selector()

        self._accept_selector.register(self._server_socket, selectors.EVENT_READ)

        self._loop_objects = (
            loopfunction.Loop(target=self._mainthread_accept_clients,
                              on_start=lambda: logging.debug('Thread started: Accept clients'),
                              on_stop=lambda: logging.debug('Thread stopped: Accept clients')),

            loopfunction.Loop(target=self._mainthread_poll_readable,
                              on_start=lambda: logging.debug('Thread started: Poll for readable clients'),
                              on_stop=lambda: logging.debug('Thread stopped: Poll for readable clients')),

        )

        self._threads_limiter = maxthreads.MaxThreads(max_subthreads)
        self.clients = []

    @Log('errors')
    def _mainthread_accept_clients(self):
        """Accepts new clients and sends them to the to _handle_accepted within a subthread
        """
        try:
            if self._accept_selector.select(timeout=self.block_time):
                sock, address = self._server_socket.accept()
                sock.setblocking(False)
                client = Client(sock, address)
                self.clients.append(client)
                logging.info('{}: New socket connection detected'.format(address))
                self._threads_limiter.start_thread(target=self._subthread_handle_accepted,
                                                   args=(client,))
        except socket.error:
            pass

    @Log('errors')
    def _mainthread_poll_readable(self):
        """Searches for readable client sockets. These sockets are then put in a subthread
        to be handled by _handle_readable
        """
        events = self._recv_selector.select(self.block_time)
        for key, mask in events:
            if mask == selectors.EVENT_READ:
                self._recv_selector.unregister(key.fileobj)

                self._threads_limiter.start_thread(target=self._subthread_handle_readable,
                                                   args=(key.fileobj,))

    @Log('errors')
    def _subthread_handle_accepted(self, client):
        """Gets accepted clients from the queue object and sets up the client socket.
        The client can then be found in the clients dictionary with the socket object
        as the key.
        """
        try:
            value = self.handle_incoming(client)
        except (socket.error, RuntimeError) as e:
            self.disconnect(client, str(e))
        else:
            if value is True and not client.closed:
                client.accepted = True
                self.register(client)
            else:
                self.disconnect(client, value)

    @Log('errors')
    def _subthread_handle_readable(self, client):
        """Handles readable client sockets. Calls the user modified handle_readable with
        the client socket as the only variable. If the handle_readable function returns
        true the client is again registered to the selector object otherwise the client
        is disconnected.
        """
        try:
            value = self.handle_readable(client)
        except (socket.error, RuntimeError) as e:
            self.disconnect(client, str(e))
        else:
            if value is True and not client.closed:
                self.register(client)
            else:
                self.disconnect(client, value)

    @Log('all')
    def start(self):
        logging.info('Binding server socket to {}:{}'.format(self.host, self.port))
        self._server_socket.bind((self.host, self.port))

        self._server_socket.listen(self.queue_size)
        logging.info('Server socket now listening (queue_size={})'.format(self.queue_size))

        logging.info('Starting main threads...')
        for loop_obj in self._loop_objects:
            loop_obj.start()

        logging.info('Main threads started')

    @Log('all')
    def stop(self):
        logging.info('Closing all ({}) connections...'.format(len(self.clients)))

        self.disconnect(self.clients, 'Server shutting down')
        logging.info('Stopping main threads...')
        for loop_obj in self._loop_objects:
            loop_obj.send_stop_signal(silent=True)

        for loop_obj in self._loop_objects:
            loop_obj.stop(silent=True)

        logging.info('Shutting down server socket...')
        self._server_socket.shutdown(socket.SHUT_RDWR)
        logging.info('Closing server socket...')
        self._server_socket.close()

    @Log('errors')
    def register(self, client, silent=False):
        try:
            self._recv_selector.register(client, selectors.EVENT_READ)
            logging.debug('{}: Registered to the selector'.format(
                client.address
            ))
        except KeyError:
            if not silent:
                logging.error(
                    '{}: Tried to register an already registered client'.format(client.address)
                )
                raise KeyError('Client already registered')

    @Log('errors')
    def unregister(self, client, silent=False):
        try:
            self._recv_selector.unregister(client)
        except KeyError:
            if not silent:
                logging.error(
                    'Tried to unregister a client that is not registered: {}'.format(client.address)
                )
                raise KeyError('Client already registered')

    @Log('errors')
    def disconnect(self, client, reason, how=socket.SHUT_RDWR):
        if hasattr(client, '__iter__'):
            if client == self.clients:
                client = self.clients.copy()
            for i in client:
                self.disconnect(i, reason, how)

        else:
            self.unregister(client, silent=True)  # will not raise errors
            client.close()

            if client in self.clients:
                self.clients.remove(client)

            logging.info('{}: Disconnected (socket closed)'.format(client.address))
            self.handle_closed(client, reason)

    def send(self, client, bytes, timeout=-1):
        """Send bytes to a client, automatically disconnects the client on error
        """
        try:
            sent = client.send(bytes, timeout)
        except (socket.error, RuntimeError) as e:
            self.disconnect(client, str(e))
            return 0
        return sent

    #
    #     return sent
    #
    # def recv(self, client, size):
    #     try:
    #         data = client.recv(size)
    #     except:
    #         self._disconnect(client)
    #         raise
    #
    #     return data