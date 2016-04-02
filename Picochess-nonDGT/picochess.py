#!/usr/bin/env python3

# Copyright (C) 2013-2016 Jean-Francois Romang (jromang@posteo.de)
#                         Shivkumar Shivaji ()
#                         Jürgen Précour (LocutusOfPenguin@posteo.de)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

import sys
import os

import configargparse
import chess
import chess.polyglot
import chess.gaviota
import chess.uci
import threading
import copy
import gc

import uci
import chesstalker.chesstalker

from timecontrol import TimeControl
from utilities import *
from keyboard import KeyboardInput, TerminalDisplay
from pgn import PgnDisplay
from server import WebServer

from dgthw import DgtHw
from dgtpi import DgtPi
from dgtvr import DgtVr
from dgtdisplay import DgtDisplay
from SensorBoard import *
from logging.handlers import RotatingFileHandler
from RPIDisplay import *

class AlternativeMover:
    def __init__(self):
        self.excludemoves = set()

    def all(self, game):
        searchmoves = set(game.legal_moves) - self.excludemoves
        if not searchmoves:
            self.reset()
            return set(game.legal_moves)
        return searchmoves

    def book(self, bookreader, game):
        try:
            bm = bookreader.weighted_choice(game, self.excludemoves)
        except IndexError:
            return None

        book_move = bm.move()
        self.add(book_move)
        g = copy.deepcopy(game)
        g.push(book_move)
        try:
            bp = bookreader.weighted_choice(g)
            book_ponder = bp.move()
        except IndexError:
            book_ponder = None
        return chess.uci.BestMove(book_move, book_ponder)

    def add(self, move):
        self.excludemoves.add(move)

    def reset(self):
        self.excludemoves = set()


def main():

    playersturn = True
    def engine_startup():
        if 'Hash' in engine.get().options:
            engine.option("Hash", args.hash_size)
        if 'Threads' in engine.get().options:  # Stockfish
            engine.option("Threads", args.threads)
        if 'Core Threads' in engine.get().options:  # Hiarcs
            engine.option("Core Threads", args.threads)
        if args.uci_option:
            for uci_option in args.uci_option.strip('"').split(";"):
                uci_parameter = uci_option.strip().split('=')
                engine.option(uci_parameter[0], uci_parameter[1])
        # send the options to the engine
        engine.send()
        # Log the engine info
        logging.debug('Loaded engine [%s]', engine_name)
        logging.debug('Supported options [%s]', engine.get().options)

    def display_system_info():
        if args.enable_internet:
            place = get_location()
            addr = get_ip()
        else:
            place = "?"
            addr = "?"
        DisplayMsg.show(Message.SYSTEM_INFO(info={"version": version, "location": place,
                                                  "books": get_opening_books(), "ip": addr,
                                                  "engine_name": engine_name, "user_name": user_name
                                                  }))

    def compute_legal_fens(g):
        """
        Computes a list of legal FENs for the given game.
        Also stores the initial position in the 'root' attribute.
        :param g: The game
        :return: A list of legal FENs, and the root FEN
        """

        class FenList(list):
            def __init__(self, *args):
                list.__init__(self, *args)
                self.root = ''

        fens = FenList()
        for move in g.legal_moves:
            g.push(move)
            fens.append(g.board_fen())
            g.pop()
        fens.root = g.board_fen()
        return fens

    def probe_tablebase(game):
        if not gaviota:
            return None
        score = gaviota.probe_dtm(game)
        if score is not None:
            Observable.fire(Event.NEW_SCORE(score='tb', mate=score))
        return score

    def think(game, tc):
        """
        Starts a new search on the current game.
        If a move is found in the opening book, fire an event in a few seconds.
        :return:
        """
        DisplayMsg.show(Message.RUN_CLOCK(turn=game.turn, time_control=tc))
        tc.run(game.turn)

        book_move = searchmoves.book(bookreader, game)
        if book_move:
            Observable.fire(Event.NEW_SCORE(score='book', mate=None))
            Observable.fire(Event.BEST_MOVE(result=book_move, inbook=True))
        else:
            probe_tablebase(game)
            engine.position(copy.deepcopy(game))
            uci_dict = tc.uci()
            uci_dict['searchmoves'] = searchmoves.all(game)
            engine.go(uci_dict)

    def analyse(game):
        """
        Starts a new ponder search on the current game.
        :return:
        """
        probe_tablebase(game)
        engine.position(copy.deepcopy(game))
        engine.ponder()

    def observe(game, tc):
        """
        Starts a new ponder search on the current game.
        :return:
        """
        DisplayMsg.show(Message.RUN_CLOCK(turn=game.turn, time_control=tc))
        tc.run(game.turn)
        analyse(game)

    def stop_search():
        """
        Stop current search.
        :return:
        """
        engine.stop()

    def stop_clock():
        nonlocal time_control
        time_control.stop()
        DisplayMsg.show(Message.STOP_CLOCK())

    def stop_search_and_clock():
        stop_clock()
        stop_search()

    def check_game_state(game, play_mode):
        """
        Check if the game has ended or not ; it also sends Message to Displays if the game has ended.
        :param game:
        :return: True is the game continues, False if it has ended
        """
        result = None
        if game.is_stalemate():
            result = GameResult.STALEMATE
        if game.is_insufficient_material():
            result = GameResult.INSUFFICIENT_MATERIAL
        if game.is_seventyfive_moves():
            result = GameResult.SEVENTYFIVE_MOVES
        if game.is_fivefold_repetition():
            result = GameResult.FIVEFOLD_REPETITION
        if game.is_checkmate():
            result = GameResult.MATE

        if result is None:
            return True
        else:
            custom_fen = getattr(game, 'custom_fen', None)
            DisplayMsg.show(Message.GAME_ENDS(result=result, play_mode=play_mode, game=copy.deepcopy(game), custom_fen=custom_fen))
            return False

    def process_fen(fen, legal_fens):
        nonlocal last_computer_fen
        if fen in legal_fens:
            # Check if we have to undo a previous move (sliding)
            if interaction_mode == Mode.NORMAL:
                if (play_mode == PlayMode.PLAY_WHITE and game.turn == chess.BLACK) or \
                        (play_mode == PlayMode.PLAY_BLACK and game.turn == chess.WHITE):
                    stop_search()
                    if game.move_stack:
                        game.pop()
            legal_moves = list(game.legal_moves)
            Observable.fire(Event.USER_MOVE(move=legal_moves[legal_fens.index(fen)]))
        elif fen == last_computer_fen:  # Player had done the computer move on the board
            last_computer_fen = None
            if check_game_state(game, play_mode) and ((interaction_mode == Mode.NORMAL) or (interaction_mode == Mode.REMOTE)):
                # finally reset all alternative moves see: handle_move()
                nonlocal searchmoves
                searchmoves.reset()
                DisplayMsg.show(Message.COMPUTER_MOVE_DONE_ON_BOARD())
                if time_control.mode != TimeMode.FIXED:
                    DisplayMsg.show(Message.RUN_CLOCK(turn=game.turn, time_control=time_control))
                    time_control.run(game.turn)
        else:  # Check if this a a previous legal position and allow user to restart from this position
            game_history = copy.deepcopy(game)
            while game_history.move_stack:
                game_history.pop()
                if (play_mode == PlayMode.PLAY_WHITE and game_history.turn == chess.WHITE) \
                        or (play_mode == PlayMode.PLAY_BLACK and game_history.turn == chess.BLACK) \
                        or (interaction_mode == Mode.OBSERVE) or (interaction_mode == Mode.KIBITZ) \
                        or (interaction_mode == Mode.REMOTE) or (interaction_mode == Mode.ANALYSIS):
                    if game_history.board_fen() == fen:
                        logging.debug("Legal Fens root       : " + str(legal_fens.root))
                        logging.debug("Current game FEN      : " + str(game.fen()))
                        logging.debug("Undoing game until FEN: " + fen)
                        stop_search()
                        while len(game_history.move_stack) < len(game.move_stack):
                            game.pop()
                        if interaction_mode == Mode.ANALYSIS or interaction_mode == Mode.KIBITZ:
                            analyse(game)
                        if interaction_mode == Mode.OBSERVE or interaction_mode == Mode.REMOTE:
                            observe(game, time_control)
                        DisplayMsg.show(Message.USER_TAKE_BACK())
                        legal_fens = compute_legal_fens(game)
                        break
        return legal_fens

    def set_wait_state():
        if interaction_mode == Mode.NORMAL:
            nonlocal play_mode
            play_mode = PlayMode.PLAY_WHITE if game.turn == chess.WHITE else PlayMode.PLAY_BLACK

    def handle_move(result, game):
        move = result.bestmove
        fen = game.fen()
        game.push(move)
        nonlocal last_computer_fen
        nonlocal searchmoves
        last_computer_fen = None
        if interaction_mode == Mode.NORMAL:
            stop_clock()
            # If UserMove: reset all alternative moves
            # If ComputerMove: disallow this move, and finally reset all if DONE_ON_BOARD event @see: process_fen()
            if (play_mode == PlayMode.PLAY_WHITE and game.turn == chess.WHITE)\
                    or (play_mode == PlayMode.PLAY_BLACK and game.turn == chess.BLACK):
                last_computer_fen = game.board_fen()
                searchmoves.add(move)
                text = Message.COMPUTER_MOVE(result=result, fen=fen, game=game.copy(), time_control=time_control)
                playersturn = False
                DisplayMsg.show(text)
            else:
                searchmoves.reset()
                DisplayMsg.show(Message.USER_MOVE(move=move, game=game.copy()))
                if check_game_state(game, play_mode):
                    think(game, time_control)

        elif interaction_mode == Mode.REMOTE:
            stop_search_and_clock()
            # If UserMove: reset all alternative moves
            # If Remote Move: same process as for computer move above
            if (play_mode == PlayMode.PLAY_WHITE and game.turn == chess.WHITE)\
                    or (play_mode == PlayMode.PLAY_BLACK and game.turn == chess.BLACK):
                last_computer_fen = game.board_fen()
                searchmoves.add(move)
                text = Message.COMPUTER_MOVE(result=result, fen=fen, game=game.copy(), time_control=time_control)
                DisplayMsg.show(text)
            else:
                searchmoves.reset()
                DisplayMsg.show(Message.USER_MOVE(move=move, game=game.copy()))
                if check_game_state(game, play_mode):
                    observe(game, time_control)

        elif interaction_mode == Mode.OBSERVE:
            stop_search_and_clock()
            DisplayMsg.show(Message.REVIEW_MOVE(move=move, fen=fen, game=game.copy(), mode=interaction_mode))
            if check_game_state(game, play_mode):
                observe(game, time_control)

        elif interaction_mode == Mode.ANALYSIS or interaction_mode == Mode.KIBITZ:
            stop_search()
            DisplayMsg.show(Message.REVIEW_MOVE(move=move, fen=fen, game=game.copy(), mode=interaction_mode))
            if check_game_state(game, play_mode):
                analyse(game)

        return game

    # Enable garbage collection - needed for engine swapping as objects orphaned
    gc.enable()

    # Command line argument parsing
    parser = configargparse.ArgParser(default_config_files=[os.path.join(os.path.dirname(__file__), "picochess.ini")])
    parser.add_argument("-e", "--engine", type=str, help="UCI engine executable path", default=None)
    parser.add_argument("-d", "--dgt-port", type=str,
                        help="enable dgt board on the given serial port such as /dev/ttyUSB0")
    parser.add_argument("-b", "--book", type=str, help="Opening book - full name of book in 'books' folder",
                        default='h-varied.bin')
    parser.add_argument("-g", "--enable-gaviota", action='store_true', help="enable gavoita tablebase probing")
    parser.add_argument("-leds", "--enable-revelation-leds", action='store_true', help="enable Revelation leds")
    parser.add_argument("-hs", "--hash-size", type=int, help="hashtable size in MB (default:64)", default=64)
    parser.add_argument("-t", "--threads", type=int, help="number of engine threads (default:1)", default=1)
    parser.add_argument("-l", "--log-level", choices=['notset', 'debug', 'info', 'warning', 'error', 'critical'],
                        default='warning', help="logging level")
    parser.add_argument("-lf", "--log-file", type=str, help="log to the given file")
    parser.add_argument("-r", "--remote", type=str, help="remote server running the engine")
    parser.add_argument("-u", "--user", type=str, help="remote user on server running the engine")
    parser.add_argument("-p", "--password", type=str, help="password for the remote user")
    parser.add_argument("-sk", "--server-key", type=str, help="key file used to connect to the remote server")
    parser.add_argument("-pgn", "--pgn-file", type=str, help="pgn file used to store the games", default='games.pgn')
    parser.add_argument("-pgn_u", "--pgn-user", type=str, help="user name for the pgn file", default=None)
    parser.add_argument("-ar", "--auto-reboot", action='store_true', help="reboot system after update")
    parser.add_argument("-web", "--web-server", dest="web_server_port", nargs="?", const=80, type=int, metavar="PORT",
                        help="launch web server")
    parser.add_argument("-mail", "--email", type=str, help="email used to send pgn files", default=None)
    parser.add_argument("-mail_s", "--smtp_server", type=str, help="Adress of email server", default=None)
    parser.add_argument("-mail_u", "--smtp_user", type=str, help="Username for email server", default=None)
    parser.add_argument("-mail_p", "--smtp_pass", type=str, help="Password for email server", default=None)
    parser.add_argument("-mail_enc", "--smtp_encryption", action='store_true',
                        help="use ssl encryption connection to smtp-Server")
    parser.add_argument("-mk", "--mailgun-key", type=str, help="key used to send emails via Mailgun Webservice",
                        default=None)
    parser.add_argument("-uci", "--uci-option", type=str, help="pass an UCI option to the engine (name;value)",
                        default=None)
    parser.add_argument("-beep", "--beep-level", type=int, help="sets a beep level from 0(=no beeps) to 15(=all beeps)",
                        default=0x0f)
    parser.add_argument("-uvoice", "--user-voice", type=str, help="voice for user", default=None)
    parser.add_argument("-cvoice", "--computer-voice", type=str, help="voice for computer", default=None)
    parser.add_argument("-inet", "--enable-internet", action='store_true', help="enable internet lookups")
    parser.add_argument("-nookmove", "--disable-ok-move", action='store_false', help="disable ok move messages")
    parser.add_argument("-v", "--version", action='version', version='%(prog)s version {}'.format(version),
                        help="show current version", default=None)
    parser.add_argument("-pi", "--dgtpi", action='store_true', help="use the dgtpi hardware")

    args = parser.parse_args()
    if args.engine is None:
        el = read_engine_ini()
        args.engine = el[0][0]  # read the first engine path and use it as standard
    else:
        args.engine = which(args.engine)

    # Enable logging
    if args.log_file:
        handler = RotatingFileHandler('logs' + os.sep + args.log_file, maxBytes=1024*1024, backupCount=9)
        logging.basicConfig(level=getattr(logging, args.log_level.upper()),
                            format='%(asctime)s.%(msecs)3d %(levelname)s %(module)s - %(funcName)s: %(message)s',
                            datefmt="%Y-%m-%d %H:%M:%S", handlers=[handler])
    logging.getLogger("chess.uci").setLevel(logging.INFO)  # don't want to get so many python-chess uci messages

    # Update
    if args.enable_internet:
        update_picochess(args.auto_reboot)

    gaviota = None
    if args.enable_gaviota:
        try:
            gaviota = chess.gaviota.open_tablebases('tablebases/gaviota')
            logging.debug('Tablebases gaviota loaded')
        except OSError:
            logging.error('Tablebases gaviota doesnt exist')
            gaviota = None

    # This class talks to DgtHw/DgtPi or DgtVr
    DgtDisplay(args.disable_ok_move).start()

    # Launch web server
    if args.web_server_port:
        WebServer(args.web_server_port).start()

    if args.dgt_port:
        # Connect to DGT board
        logging.debug("Starting picochess with DGT board on [%s]", args.dgt_port)
        if args.dgtpi:
            dgthardware = DgtPi(args.dgt_port, args.enable_revelation_leds, args.beep_level)
        else:
            dgthardware = DgtHw(args.dgt_port, args.enable_revelation_leds, args.beep_level)
    else:
        # Enable keyboard input and terminal display
        logging.debug("Starting picochess with virtual DGT board")
        arduino = serial.Serial('COM6', 115200,timeout=.1)
        KeyboardInput().start()
        SensorBoard(arduino).start()
        TerminalDisplay(arduino).start()
        RpiDisplay().start()
        dgthardware = DgtVr(args.enable_revelation_leds, args.beep_level)
    # Start the show
    dgthardware.start()

    # Save to PGN
    PgnDisplay(
        args.pgn_file, net=args.enable_internet, email=args.email, mailgun_key=args.mailgun_key,
        smtp_server=args.smtp_server, smtp_user=args.smtp_user,
        smtp_pass=args.smtp_pass, smtp_encryption=args.smtp_encryption).start()
    if args.pgn_user:
        user_name = args.pgn_user
    else:
        if args.email:
            user_name = args.email.split('@')[0]
        else:
            user_name = "Player"

    # Create ChessTalker for speech output
    talker = None
    if args.user_voice or args.computer_voice:
        logging.debug("Initializing ChessTalker [%s, %s]", str(args.user_voice), str(args.computer_voice))
        talker = chesstalker.chesstalker.ChessTalker(args.user_voice, args.computer_voice)
        talker.start()
    else:
        logging.debug("ChessTalker disabled")

    # Gentlemen, start your engines...
    engine = uci.Engine(args.engine, hostname=args.remote, username=args.user,
                        key_file=args.server_key, password=args.password)
    try:
        engine_name = engine.get().name
    except AttributeError:
        logging.error("No engines started")
        sys.exit(-1)

    # Startup - internal
    game = chess.Board()  # Create the current game
    legal_fens = compute_legal_fens(game)  # Compute the legal FENs
    all_books = get_opening_books()
    try:
        book_index = [book[1] for book in all_books].index('books/' + args.book)
    except ValueError:
        logging.warning("Selected book not present, defaulting to %s", all_books[7][1])
        book_index = 7
    bookreader = chess.polyglot.open_reader(all_books[book_index][1])
    searchmoves = AlternativeMover()
    interaction_mode = Mode.NORMAL
    play_mode = PlayMode.PLAY_WHITE
    time_control = TimeControl(TimeMode.BLITZ, minutes_per_game=5)
    last_computer_fen = None
    game_declared = False  # User declared resignation or draw

    system_info_thread = threading.Timer(0, display_system_info)
    system_info_thread.start()
    engine_startup()  # send the args options to the engine

    # Startup - external
    text = Dgt.DISPLAY_TEXT(l=None, m='bl   5', s=None, beep=BeepLevel.NO, duration=0)
    DisplayMsg.show(Message.STARTUP_INFO(info={"interaction_mode": interaction_mode, "play_mode": play_mode,
                                               "book": all_books[book_index][1], "book_index": book_index,
                                               "time_text": text}))
    DisplayMsg.show(Message.UCI_OPTION_LIST(options=engine.options))
    DisplayMsg.show(Message.ENGINE_STARTUP(shell=engine.get_shell(), path=engine.get_path(),
                                           has_levels=engine.has_levels(), has_960=engine.has_chess960()))

    # Event loop
    while True:
        try:
            event = evt_queue.get()
        except queue.Empty:
            pass
        else:
            logging.debug('Received event from evt queue: %s', event)
            for case in switch(event):
                if case(EventApi.FEN):
                    legal_fens = process_fen(event.fen, legal_fens)
                    break

                if case(EventApi.KEYBOARD_MOVE):
                    move = event.move
                    logging.debug('Keyboard move [%s]', move)
                    if move not in game.legal_moves:
                        logging.warning('Illegal move [%s]', move)
                    else:
                        g = copy.deepcopy(game)
                        g.push(move)
                        legal_fens = process_fen(g.board_fen(), legal_fens)
                    break

                if case(EventApi.USER_MOVE):
                    move = event.move
                    logging.debug('User move [%s]', move)
                    if move not in game.legal_moves:
                        logging.warning('Illegal move [%s]', move)
                    else:
                        result = chess.uci.BestMove(bestmove=move, ponder=None)
                        game = handle_move(result, game)
                        # if check_game_state(game, interaction_mode):
                        legal_fens = compute_legal_fens(game)
                    break

                if case(EventApi.LEVEL):
                    logging.debug("Setting engine to level %i", event.level)
                    if engine.level(event.level):
                        engine.send()
                        DisplayMsg.show(Message.LEVEL(level=event.level, level_text=event.level_text))
                    break

                if case(EventApi.NEW_ENGINE):
                    old_path = engine.path
                    engine_shutdown = True
                    # Stop the old engine cleanly
                    engine.stop()
                    # Closeout the engine process and threads
                    # The all return non-zero error codes, 0=success
                    if engine.quit():  # Ask nicely
                        if engine.terminate():  # If you won't go nicely.... 
                            if engine.kill():  # Right that does it!
                                logging.error('Engine shutdown failure')
                                DisplayMsg.show(Message.ENGINE_FAIL())
                                engine_shutdown = False
                    if engine_shutdown:
                        # Load the new one and send args.
                        # Local engines only
                        engine_fallback = False
                        engine = uci.Engine(event.eng[0])
                        try:
                            engine_name = engine.get().name
                        except AttributeError:
                            # New engine failed to start, restart old engine
                            logging.error("New engine failed to start, reverting to %s", old_path)
                            engine_fallback = True
                            engine = uci.Engine(old_path)
                            try:
                                engine_name = engine.get().name
                            except AttributeError:
                                # Help - old engine failed to restart. There is no engine
                                logging.error("No engines started")
                                sys.exit(-1)
                        # Schedule cleanup of old objects
                        gc.collect()
                        # Restore options - this doesn't deal with any
                        # supplementary uci options sent 'in game', see event.UCI_OPTION_SET
                        engine_startup()
                        # Send user selected engine level to new engine
                        if event.level and engine.level(event.level):
                            engine.send()
                            DisplayMsg.show(Message.LEVEL(level=event.level, level_text=event.level_text))
                        # All done - rock'n'roll
                        if not engine_fallback:
                            DisplayMsg.show(Message.ENGINE_READY(eng=event.eng, engine_name=engine_name,
                                                                 eng_text=event.eng_text,
                                                                 has_levels=engine.has_levels(),
                                                                 has_960=engine.has_chess960()))
                        else:
                            DisplayMsg.show(Message.ENGINE_FAIL())
                        set_wait_state()
                        DisplayMsg.show(Message.WAIT_STATE())
                        # Go back to analysing or observing
                        if interaction_mode == Mode.ANALYSIS or interaction_mode == Mode.KIBITZ:
                            analyse(game)
                        if interaction_mode == Mode.OBSERVE or interaction_mode == Mode.REMOTE:
                            observe(game, time_control)
                    break

                if case(EventApi.SETUP_POSITION):
                    logging.debug("Setting up custom fen: {0}".format(event.fen))
                    if engine.has_chess960():
                        engine.option('UCI_Chess960', event.uci960)
                        engine.send()
                    else:  # start normal new game if engine can't handle the user wish
                        event.uci960 = False
                        logging.warning('Engine doesnt support 960 mode')
                    if game.move_stack:
                        if game.is_game_over() or game_declared:
                            custom_fen = getattr(game, 'custom_fen', None)
                            DisplayMsg.show(Message.GAME_ENDS(result=GameResult.ABORT, play_mode=play_mode,
                                                              game=copy.deepcopy(game), custom_fen=custom_fen))
                    game = chess.Board(event.fen, event.uci960)
                    game.custom_fen = event.fen
                    legal_fens = compute_legal_fens(game)
                    stop_search_and_clock()
                    time_control.reset()
                    interaction_mode = Mode.NORMAL
                    last_computer_fen = None
                    searchmoves.reset()
                    DisplayMsg.show(Message.START_NEW_GAME())
                    game_declared = False
                    set_wait_state()
                    DisplayMsg.show(Message.WAIT_STATE())
                    break

                if case(EventApi.STARTSTOP_THINK):
                    if engine.is_thinking() and (interaction_mode != Mode.REMOTE):
                        stop_clock()
                        engine.stop(show_best=True)
                    else:
                        play_mode = PlayMode.PLAY_WHITE if play_mode == PlayMode.PLAY_BLACK else PlayMode.PLAY_BLACK
                        DisplayMsg.show(Message.PLAY_MODE(play_mode=play_mode))
                        if check_game_state(game, play_mode) and (interaction_mode != Mode.REMOTE):
                            think(game, time_control)
                    break

                if case(EventApi.ALTERNATIVE_MOVE):
                    game.pop()
                    DisplayMsg.show(Message.ALTERNATIVE_MOVE())
                    think(game, time_control)
                    break

                if case(EventApi.STARTSTOP_CLOCK):
                    if time_control.is_ticking():
                        stop_clock()
                    else:
                        DisplayMsg.show(Message.RUN_CLOCK(turn=game.turn, time_control=time_control))
                        time_control.run(game.turn)
                    break

                if case(EventApi.NEW_GAME):
                    if game.move_stack:
                        logging.debug("Starting a new game")
                        if not (game.is_game_over() or game_declared):
                            custom_fen = getattr(game, 'custom_fen', None)
                            DisplayMsg.show(Message.GAME_ENDS(result=GameResult.ABORT, play_mode=play_mode,
                                                              game=copy.deepcopy(game), custom_fen=custom_fen))
                        game = chess.Board()
                    legal_fens = compute_legal_fens(game)
                    # interaction_mode = Mode.NORMAL @todo
                    last_computer_fen = None
                    stop_search_and_clock()
                    time_control.reset()
                    searchmoves.reset()
                    DisplayMsg.show(Message.START_NEW_GAME(time_control=time_control))
                    game_declared = False
                    set_wait_state()
                    DisplayMsg.show(Message.WAIT_STATE())
                    break

                if case(EventApi.DRAWRESIGN):
                    if not game_declared:  # in case user leaves kings in place while moving other pieces
                        custom_fen = getattr(game, 'custom_fen', None)
                        DisplayMsg.show(Message.GAME_ENDS(result=event.result, play_mode=play_mode,
                                                          game=copy.deepcopy(game), custom_fen=custom_fen))
                        game_declared = True
                    break

                if case(EventApi.REMOTE_MOVE):
                    if interaction_mode == Mode.REMOTE:
                        bm = chess.uci.BestMove(bestmove=chess.Move.from_uci(event.move), ponder=None)
                        game = handle_move(bm, game)
                        legal_fens = compute_legal_fens(game)
                    break

                if case(EventApi.BEST_MOVE):
                    if event.inbook:
                        DisplayMsg.show(Message.BOOK_MOVE(result=event.result))
                    game = handle_move(event.result, game)
                    playersturn = False
                    legal_fens = compute_legal_fens(game)
                    break

                if case(EventApi.NEW_PV):
                    if interaction_mode == Mode.NORMAL:
                        pass
                    else:
                        DisplayMsg.show(Message.NEW_PV(pv=event.pv, mode=interaction_mode, fen=game.fen()))
                    break

                if case(EventApi.NEW_SCORE):
                    if event.score == 'book':
                        score = 'book'
                    elif event.score == 'tb':
                        score = 'tb {0}'.format(event.mate)
                    else:
                        try:
                            score = int(event.score)
                            if game.turn == chess.BLACK:
                                score *= -1
                        except ValueError:
                            score = event.score
                            logging.debug('Could not convert score ' + score)
                        except TypeError:
                            score = 'm {0}'.format(event.mate)
                    DisplayMsg.show(Message.NEW_SCORE(score=score, mate=event.mate, mode=interaction_mode))
                    break

                if case(EventApi.SET_INTERACTION_MODE):
                    if interaction_mode == Mode.NORMAL or interaction_mode == Mode.OBSERVE or interaction_mode == Mode.REMOTE:
                        stop_clock()  # only stop, if the clock is really running
                    interaction_mode = event.mode
                    if engine.is_thinking():
                        stop_search()  # dont need to stop, if pondering
                    if engine.is_pondering() and interaction_mode == Mode.NORMAL:
                        stop_search()  # if change from ponder modes to game, also stops the pondering
                    set_wait_state()
                    DisplayMsg.show(Message.INTERACTION_MODE(mode=event.mode, mode_text=event.mode_text))
                    break

                if case(EventApi.SET_OPENING_BOOK):
                    logging.debug("Changing opening book [%s]", event.book[1])
                    bookreader = chess.polyglot.open_reader(event.book[1])
                    DisplayMsg.show(Message.OPENING_BOOK(book_name=event.book[0], book_text=event.book_text))
                    break

                if case(EventApi.SET_TIME_CONTROL):
                    tc = event.time_control
                    #time_control = TimeControl(ClockMode.BLITZ, minutes_per_game=5)
                    #TimeControl(ClockMode.BLITZ, minutes_per_game=1)
                    #print("------BL-----event TC",Message.TIME_CONTROL(time_control_string=event.time_control_string, beep=event.beep))
                    DisplayMsg.show(Message.TIME_CONTROL(time_text=event.time_text,time_control=event.time_control))
                    break

                if case(EventApi.OUT_OF_TIME):
                    stop_search_and_clock()
                    custom_fen = getattr(game, 'custom_fen', None)
                    DisplayMsg.show(Message.GAME_ENDS(result=GameResult.OUT_OF_TIME, play_mode=play_mode,
                                                      game=copy.deepcopy(game), custom_fen=custom_fen))
                    break

                if case(EventApi.UCI_OPTION_SET):
                    # Nowhere calls this yet, but they will need to be saved for engine restart
                    engine.option(event.name, event.value)
                    break

                if case(EventApi.SHUTDOWN):
                    if talker:
                        talker.say_event(event)
                    custom_fen = getattr(game, 'custom_fen', None)
                    DisplayMsg.show(Message.GAME_ENDS(result=GameResult.ABORT, play_mode=play_mode,
                                                      game=copy.deepcopy(game), custom_fen=custom_fen))
                    shutdown()
                    break

                if case(EventApi.REBOOT):
                    if talker:
                        talker.say_event(event)
                    custom_fen = getattr(game, 'custom_fen', None)
                    DisplayMsg.show(Message.GAME_ENDS(result=GameResult.ABORT, play_mode=play_mode,
                                                      game=copy.deepcopy(game), custom_fen=custom_fen))
                    reboot()
                    break

                if case(EventApi.DGT_BUTTON):
                    DisplayMsg.show(Message.DGT_BUTTON(button=event.button))
                    break
                if case(EventApi.Lift_Piece):
                    from_square = int(event.square)
                    if playersturn:
                        print(" possible moves ")
                        mvlist = []
                        for move in game.legal_moves:
                            if move.from_square == from_square:
                                mvlist.append(move.to_square)
                                DisplayMsg.show(Message.LIGHT_SQUARE(square = mvlist, on = True))
                                print(move.to_square)
                    break
                if case(EventApi.Drop_Piece):
                    if playersturn:
                        playermove = chess.SQUARE_NAMES[int(from_square)]+ chess.SQUARE_NAMES[int(event.square)]
                        move = chess.Move.from_uci(playermove)
                        Observable.fire(Event.KEYBOARD_MOVE(move=move))
                    DisplayMsg.show(Message.LIGHT_SQUARE(square = mvlist, on = False))
                    DisplayMsg.show(Message.COMPUTER_MOVE_DONE_ON_BOARD())

                    playersturn = True

                    break

                if case(EventApi.DGT_FEN):
                    DisplayMsg.show(Message.DGT_FEN(fen=event.fen))
                    break

                if case():  # Default
                    logging.warning("Event not handled : [%s]", event)

            evt_queue.task_done()


if __name__ == '__main__':
    main()
