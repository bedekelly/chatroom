#!/usr/bin/env python3
"""
A mini chatroom server written in Twisted.
"""

import re
import pickle
from os.path import expanduser as abs_path
from twisted.internet import reactor, protocol
from twisted.protocols.basic import LineReceiver

PORT = 8001
DEFAULT_ADMIN_PASS = "open_sesame"
USERS_FILE = abs_path("~/.chatroom_users.dat")
OP_CMDS = ["/kick", "/ban", "/unban", "/kickall", "/op"]


def requires_op(cmd):
    """
    Determine whether a command requires the user to be an operator.
    """
    return cmd in OP_CMDS

    
class states:
    """
    Enumerate all states that an IOStream protocol instance can be in.
    """
    REGISTERED = 0
    REQUESTING_NAME = 1
    REQUESTING_PASSWORD = 2
    REQUESTING_NEW_PASSWORD = 3
    CHANGING_USERNAME = 4
    CHOOSING_KICK_OTHER_SESS = 5


class UserSession(LineReceiver):
    """
    Represent a single user-session. Handles registering, logins, sending
    messages etc.
    """
    def __init__(self, factory, addr):
        self.factory = factory
        self.muted = False
        self.addr = addr
        self.reason_for_quit = None
        self.name = "Anonymous"
        self.is_op = False

    def connectionMade(self):
        """
        When we get a new connection, request the user's name.
        """
        print("{} connected.".format(self.longname))
        self.sendLine("What is your name? (Type below and press Return)")
        self.loseConnection = self.transport.loseConnection
        self.state = states.REQUESTING_NAME

    def connectionLost(self, _):
        """
        When we lose a connection, let the other users know.
        """
        msg = "{} lost connection.".format(self.longname)
        if self.reason_for_quit:
            msg += ' ("{}")'.format(self.reason_for_quit)
        print(msg)
        if not self.muted:
            self.broadcast_noti(msg)
        self.factory.remove_connection(self)

    def sendLine(self, line):
        """
        Wrap the LineReceiver's sendLine method to use strings instead of bytes.
        """
        super().sendLine(bytes(line, encoding="utf-8"))

    def lineReceived(self, line):
        """
        When we get a new line, check what state we're in and act accordingly.
        """
        try:
            line = line.decode("utf-8").strip()
        except UnicodeDecodeError:
            # Just don't even try to handle it.
            return

        line_callbacks = {
            states.REGISTERED: self.command_or_msg,
            states.REQUESTING_NAME: self.got_username,
            states.REQUESTING_PASSWORD: self.got_password,
            states.REQUESTING_NEW_PASSWORD: self.got_new_password,
            states.CHOOSING_KICK_OTHER_SESS: self.got_kick_choice
            }
        
        line_callbacks[self.state](line)

    def command_or_msg(self, line):
        """
        Check if a line sent from a registered user is a command or a message.
        If it's a command, do what they say.
        If it's a message, broadcast it to the other users.
        """
        is_command = lambda l: l.startswith("/")
        if is_command(line):
            self.handle_command(line)
        elif not self.muted:
            self.broadcast_msg(line)

    def got_username(self, uname):
        """
        Called when the user has entered their username.
        Check whether the username is valid, and if so, continue with the login
        or registration (after checking that the username is not currently
        mapped to a session).
        """
        valid = re.compile("^[A-Za-z0-9_]+$")
        is_valid = lambda attempt: bool(valid.fullmatch(attempt))
        if not is_valid(uname):
            self.sendLine("Please enter a valid username."
                          "(Type below and press Return)")
            return
        if uname in self.factory.online_users:
            self.sendLine("This account is being accessed somewhere else.")
            self.sendLine("Kick the other account? [Y/N]")
            self.requested_uname = uname
            self.state = states.CHOOSING_KICK_OTHER_SESS
        elif uname in self.factory.users:
            self.request_password(uname)
        else:
            self.request_new_password(uname)

    def got_password(self, pword):
        """
        Checks the user's password against the value stored for that username.
        If it's right, allow them in. If not, just stay in the same state.
        """
        user_data = self.factory.users[self.requested_uname]
        if user_data["pword"] == pword:
            self.is_op = user_data["is_op"]
            # Kick any session with our name, then set our name.
            self.kick_other_sessions()
            self.name = self.requested_uname
            self.state = states.REGISTERED
            self.factory.online_users[self.name] = self
            self.welcome()
        else:
            self.sendLine("Password incorrect. Enter password:")

    def got_new_password(self, pword):
        """
        Make an account using the requested username and password.
        Welcome the user and log their entrance.
        """
        self.name = self.requested_uname
        self.factory.users[self.name] = {'pword': pword, "is_op": False}
        self.factory.online_users[self.name] = self
        self.state = states.REGISTERED
        self.sendLine("Account created successfully.")
        print("Anonymous({}) registered as {}"
              "".format(self.addr.host, self.name))
        # Display some personalised welcome information.
        self.welcome()
        # Output some debug info.
        # Save our changes.
        save_users(self.factory.users)

    def got_kick_choice(self, yesno):
        """
        Determine whether the user wants to kick their other session and act
        accordingly.
        """
        if yesno in ("y", "Y"):
            self.sendLine("Enter password:")
            self.state = states.REQUESTING_PASSWORD
        elif yesno in ("n", "N"):
            self.sendLine("Enter name:")
            self.state = states.REQUESTING_NAME
        else:
            # Don't change state: the next line should be sent to this function.
            self.sendLine("Enter Y or N: "
                          "kick the other session using this account?")

    def request_password(self, uname):
        """
        Called when the requested username is preexisting.
        """
        self.sendLine("Password:")
        self.requested_uname = uname
        self.state = states.REQUESTING_PASSWORD

    def msg_format(self, line):
        """
        When we're sending a message, prepend a nicely-formatted version of our
        username and hostname.
        """
        return "[{}] {}".format(self.name, line.strip())

    def broadcast_msg(self, line):
        """
        Send a formatted message to each connected user except this one.
        """
        self.factory.broadcast_message(self.msg_format(line), exception=self)

    def broadcast_noti(self, line):
        """
        Send a formatted server notification to each connected user except this
        one.
        """
        self.factory.broadcast_message(line, exception=self)

    def handle_command(self, text):
        """
        Process a command like /quit or /leave.
        """
        cmd, *params = text.split()
        print("Command from {}: {}".format(self.longname, text))

        if requires_op(cmd) and not self.is_op:
            self.sendLine("You are not OP.")
            return
        if cmd in ("/quit", "/leave"):
            reason = ' '.join(params)
            self.reason_for_quit = reason
            self.transport.loseConnection()
        elif cmd in ("/nick", "/user", "/username"):
            if params:
                self.got_username('_'.join(params))
            else:
                self.sendLine("Usage: {} <new nick>".format(cmd))
        elif cmd == "/me":
            if not params:
                self.sendLine("! usage: /me <action>")
            elif not self.muted:
                msg = "* {} {}".format(self.name,' '.join(params))
                self.factory.broadcast_message(msg)
        elif cmd == "/kick":
            if not params:
                self.sendLine("! usage: /kick <username> ...")
            for user in params:
                self.factory.kick(user, kicked_by=self)
        elif cmd == "/op":
            if not params:
                self.sendLine("! usage: /op <username> ...")
            for user in params:
                self.factory.op(user, requester=self)
        elif cmd == "/deop":
            if not params:
                self.sendLine("! usage: /deop <username> ...")
            for user in params:
                self.factory.deop(user, requester=self)


    def welcome(self):
        """
        Welcome the user to the chatroom.
        Show how many users there are, and let the others know.
        """
        print("Anonymous({}) logged in as {}"
              "".format(self.addr.host, self.name))

        self.sendLine("Welcome {}!".format(self.name))
        self.sendLine("{} people online currently."
                      "".format(self.factory.num_users))
        if not self.muted:
            self.broadcast_noti("{} joined the chatroom."
                                "".format(self.longname))

    def kick_other_sessions(self):
        """
        Kick any connection with the same name as us.
        """
        self.factory.kick_by_name(self.requested_uname)
        

    def request_new_password(self, uname):
        """
        Called when the requested username is new.
        """
        self.requested_uname = uname
        self.sendLine("Enter new password:")
        self.state = states.REQUESTING_NEW_PASSWORD

    @property
    def longname(self):
        """
        Formats the username and host address.
        """
        return "{}({}:{})".format(self.name, self.addr.host, self.addr.port)


class UserSessionFactory(protocol.Factory):
    """
    Handle building and managing all the server's connections.
    """
    def __init__(self):
        self.online_users = {}
        self.connections = set()
        self.users = load_users()

    def op(self, username, requester):
        """
        Make a currently-online user OP, or send the requester a failure message.
        """
        try:
            user = self.online_users[username]
        except KeyError:
            requester.sendLine("! That user isn't online right now.")
        else:
            self.broadcast_message("! {} is now OP.".format(username),
                                   exception=user)
            user.sendLine("! You are now OP.")
            user.is_op = True
            self.users[username]["is_op"] = True
            save_users(self.users)

    def deop(self, username, requester):
        """
        Remove a currently-online user's OP status, or send the requester a
        failure message.
        """
        try:
            user = self.online_users[username]
        except KeyError:
            requester.sendLine("! The user isn't online right now.")
        else:
            self.broadcast_message("! {} is no longer OP.".format(username),
                                   exception=user)
            user.sendLine("! You are no longer OP.")
            self.users[username]["is_op"] = False
            user.is_op = False
            save_users(self.users)

    def kick_by_name(self, username, kicked_by=None):
        """
        Kick a user by providing their username.
        """
        try:
            conn = self.online_users[username]
        except KeyError:
            if kicked_by is not None:
                kicked_by.sendLine("! That user isn't online right now.")
        else:
            self.kick(conn, kicked_by=kicked_by)

    def kick(self, conn, kicked_by=None):
        """
        Kick a user currently on the channel directly by their connection.
        """
        conn.muted = True  # Prevent "lost connection" message.
        conn.transport.loseConnection()
        msg = "! {} was kicked".format(conn.name)
        if kicked_by is not None:
            msg += " by {}".format(kicked_by.name)

    @property
    def num_users(self):
        """
        Return the number of currently online users.
        """
        return len(self.online_users)

    def remove_connection(self, conn):
        """
        Remove a connection from our current server state.
        """
        self.connections.remove(conn)
        try:
            del self.online_users[conn.name]
        except KeyError:
            # No problem, the connection wasn't registered.
            pass

    def broadcast_message(self, message, exception=None):
        """
        Broadcast `message` to every connected user except `exception`.
        """
        for conn in self.connections:
            if conn is not exception and conn.state == states.REGISTERED:
                conn.sendLine(message)

    def buildProtocol(self, addr):
        """
        Create and return a new connection instance, adding it to a dict of
        users.
        """
        sess = UserSession(self, addr)
        self.connections.add(sess)
        return sess


def save_users(users):
    """
    Save the user-password mapping to the local folder.
    """
    with open(USERS_FILE, "wb") as users_file:
        pickle.dump(users, users_file)


def load_users():
    """
    Attempt to load users from the data file.
    If one doesn't exist, just return a new mapping.
    """
    try:
        with open(USERS_FILE, "rb") as f:
            return pickle.load(f)
    except FileNotFoundError:
        return {"admin": {"pword": DEFAULT_ADMIN_PASS,
                          "is_op": True}}


reactor.listenTCP(PORT, UserSessionFactory())
print("Server running at localhost:{p}."
      "Connect using `telnet localhost {p}`.".format(p=PORT))
reactor.run()
