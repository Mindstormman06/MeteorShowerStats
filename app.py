from flask import Flask, render_template, jsonify
import json
import os
from PlayerGrabber import PlayerGrabber
from MinecraftStatsHandler import MinecraftStatsHandler
from advancement_criteria_generator import build_multi_part_advancements
import zipfile
import threading
import tempfile
import time
from datetime import datetime
from mcstatus import JavaServer
import nbtlib
import logging
import socket
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

app = Flask(__name__)

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

STATS_DIR = os.path.join(BASE_DIR, 'output_data', 'simplified_stats')

DATA_FILE_PATH = os.path.join(BASE_DIR, 'output_data', 'usernames.json')

SERVER_INFO_FILE_PATH = os.path.join(BASE_DIR, 'output_data', 'server_info.json')

SERVER_ADDRESS = "localhost"

# Default ports (may be overridden by config)
MC_SERVER_PORT = 25565
FLASK_PORT = 5000

FILE_PATH = "../logs/latest.log"

# Create a threading event to signal when to stop
stop_event = threading.Event()

# Suppress Werkzeug logs
logging.getLogger('werkzeug').setLevel(logging.ERROR)

# Get the local IP address
my_ip = socket.gethostbyname(socket.gethostname())

_state_lock = threading.Lock()
def load_server_info():
    if not os.path.exists(SERVER_INFO_FILE_PATH):
        return {}

    try:
        with open(SERVER_INFO_FILE_PATH, "r") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        # Corrupt or unreadable file → fail safe
        return {}

def save_server_info(state: dict):
    os.makedirs(os.path.dirname(SERVER_INFO_FILE_PATH), exist_ok=True)

    with _state_lock:
        dir_name = os.path.dirname(SERVER_INFO_FILE_PATH)

        # Write atomically
        with tempfile.NamedTemporaryFile(
            "w",
            dir=dir_name,
            delete=False
        ) as tmp:
            json.dump(state, tmp, indent=2)
            tmp.flush()
            os.fsync(tmp.fileno())

        # Atomic replace (POSIX safe)
        os.replace(tmp.name, SERVER_INFO_FILE_PATH)

def jar_has_assets(jar_path: str) -> bool:
    try:
        with zipfile.ZipFile(jar_path) as zf:
            # Look for assets/ directory entry
            return any(
                name.startswith("assets/")
                for name in zf.namelist()
            )
    except zipfile.BadZipFile:
        return False

def find_latest_jar(versions_dir="../versions"):
    # ---------- Try versions/ directory ----------
    if os.path.isdir(versions_dir):
        for version in sorted(os.listdir(versions_dir), reverse=True):
            version_dir = os.path.join(versions_dir, version)
            if not os.path.isdir(version_dir):
                continue

            for f in os.listdir(version_dir):
                if f.endswith(".jar"):
                    jar_path = os.path.join(version_dir, f)
                    if jar_has_assets(jar_path):
                        return jar_path

    # ---------- Fallback: parent directory ----------
    parent_dir = os.path.abspath(os.path.join(os.getcwd(), ".."))
    if os.path.isdir(parent_dir):
        for f in sorted(os.listdir(parent_dir), reverse=True):
            if f.endswith(".jar"):
                jar_path = os.path.join(parent_dir, f)
                if jar_has_assets(jar_path):
                    return jar_path

    raise FileNotFoundError(
        "No Minecraft server JAR with assets/ found in versions/ or parent directory"
    )

def run_initial_processing():
    """
    Runs PlayerGrabber and MinecraftStatsHandler to process data when the app starts.
    """
    print("Starting initial data processing...")
    
    latest_server_jar = None
    while not latest_server_jar:
        try:
            latest_server_jar = find_latest_jar("../versions")
        except:
            print("trying again to find server jar")
            time.sleep(5)
    
    build_multi_part_advancements(latest_server_jar, "static/advancement_criteria.json")
    
    file_path = "./output_data/usernames.json"
    default_data = {
        "usermap": {},
        "uuids": [],
        "usernames": []
    }

    # Make sure the folder exists
    os.makedirs(os.path.dirname(file_path), exist_ok=True)

    # Check if file exists and has content
    if not os.path.isfile(file_path) or os.path.getsize(file_path) == 0:
        # File missing or empty → create with default data
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(default_data, f, indent=4)

    
def data_loop():
    """
    Runs PlayerGrabber and MinecraftStatsHandler to process data while the app runs.
    """
    global stop_event
    # Process player data using PlayerGrabber
    input_folder = "../world/playerdata"
    output_folder = "./output_data/playerdata"
    os.makedirs(output_folder, exist_ok=True)
    while not stop_event.is_set():
        try:
            for file in os.listdir(input_folder):
                if file.endswith(".dat"):
                    input_file = os.path.join(input_folder, file)
                    output_file = os.path.join(output_folder, f"{file.split('.')[0]}.json")
                    grabber = PlayerGrabber(input_file, output_file)
                    grabber.process_data()

            # Process stats and advancements using MinecraftStatsHandler
            try:
                dummy = MinecraftStatsHandler("dummy_uuid")
                for player_uuid in dummy.folder:
                    try:
                        player = MinecraftStatsHandler(player_uuid)
                        result = player.get_minecraft_usernames()
                        if isinstance(result, dict) and "name" in result:
                            # print(f"Username: {result['name']}")
                            player.get_minecraft_skins()

                            # Grab cape from capes.dev
                            player.get_minecraft_capes()

                            # Generate advancement report
                            player.generate_achievement_report()

                            # Convert stats to simplified JSON
                            player.convert_stats_to_simplified_json()
                            #print("")
                        else:
                            #print(result)
                            pass
                    except Exception as e:
                        print(f"An error occurred with UUID {player_uuid}: {e}")
                dummy.write_playerdata()
            except Exception as main_e:
                print(f"An error occurred in the main execution: {main_e}")
            # Sleep between cycles if no stop event is set
            if not stop_event.is_set():
                time.sleep(5)
        except Exception as e:
            print(f"An error occurred during data processing: {e}")
            break  # In case of an unexpected error, exit the loop gracefully
    print("Initial data processing complete.")


# Load data from JSON
def load_data():
    with open(DATA_FILE_PATH, 'r') as f:
        data = json.load(f)
    
    # Convert the "usermap" dictionary into a list of player dictionaries
    players = [{"uuid": uuid, "username": username} for uuid, username in data["usermap"].items()]
    
    return players

def load_player_stats(uuid):
    stats_file = os.path.join(STATS_DIR, f"simplified_stats_{uuid}.json")
    if os.path.exists(stats_file):
        with open(stats_file, 'r') as f:
            return json.load(f)
    return None

def load_advancements(player_uuid):
    advancements_file_path = os.path.join(BASE_DIR, 'output_data/advancements_reports', f'advancements_report_{player_uuid}.json')
    try:
        with open(advancements_file_path, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        return None


with open('StatCrafterConfig.json') as config_file:
    config_data = json.load(config_file)

# Apply configuration overrides
SERVER_ADDRESS = os.getenv("SERVER_ADDRESS", config_data.get("minecraft_server_address", SERVER_ADDRESS))
MC_SERVER_PORT = config_data.get("minecraft_server_port", MC_SERVER_PORT)
FLASK_PORT = config_data.get("port", FLASK_PORT)
SERVER_HOST = config_data.get("server_ip", '0.0.0.0')
TIMEZONE = config_data.get("timezone", None)
try:
    tzinfo = ZoneInfo(TIMEZONE) if (TIMEZONE and ZoneInfo) else None
except Exception:
    tzinfo = None

    
# Server Clock
def calculate_uptime():
    try:
        state = load_server_info()
        last_start = state.get("last_start")
        last_stop = state.get("last_stop")

        # --- Try updating from latest.log ---
        if os.path.exists(FILE_PATH):
            try:
                with open(FILE_PATH, "r") as f:
                    lines = f.readlines()

                # Read bottom-up
                for line in reversed(lines):
                    if "Starting minecraft server version" in line:
                        # --- time ---
                        ts = line.split("]")[0].strip("[")
                        found = datetime.strptime(ts, "%H:%M:%S")

                        now = datetime.now(tzinfo) if tzinfo else datetime.now()
                        found = found.replace(
                            year=now.year,
                            month=now.month,
                            day=now.day,
                            tzinfo=tzinfo
                        )

                        iso = found.isoformat()

                        # --- version ---
                        # Everything after "version "
                        version = line.split("version", 1)[1].strip()

                        if iso != last_start:
                            state["last_start"] = iso
                            state["last_stop"] = None
                            state["server_up"] = True
                            state["server_version"] = version
                            save_server_info(state)

                        last_start = iso
                        last_stop = None
                        break


                    elif "Stopping server" in line:
                        ts = line.split("]")[0].strip("[")
                        found = datetime.strptime(ts, "%H:%M:%S")

                        now = datetime.now(tzinfo) if tzinfo else datetime.now()
                        found = found.replace(
                            year=now.year,
                            month=now.month,
                            day=now.day,
                            tzinfo=tzinfo
                        )

                        iso = found.isoformat()
                        if iso != last_stop:
                            state["last_stop"] = iso
                            state["server_up"] = False
                            save_server_info(state)

                        last_stop = iso
                        break

            except Exception:
                pass  # log parsing failed → fall back to stored state

        # --- Calculate uptime from stored state ---
        if not last_start or not state.get("server_up", False):
            return "Offline"

        start_dt = datetime.fromisoformat(last_start)

        end_time = (datetime.now(tzinfo) if tzinfo else datetime.now())
        uptime = end_time - start_dt

        days = uptime.days
        hours, rem = divmod(uptime.seconds, 3600)
        minutes, seconds = divmod(rem, 60)

        return f"{days} Days, {hours:02}:{minutes:02}:{seconds:02}"

    except Exception as e:
        return f"Error: {e}"


@app.route("/uptime", methods=["GET"])
def get_uptime():
    """API endpoint to get the server's uptime."""
    uptime = calculate_uptime()
    return jsonify({"uptime": uptime})


# Player Counter
@app.route('/live_player_count', methods=['GET'])
def live_player_count():
    """Fetch the current player count from the Minecraft server."""
    try:
        server = JavaServer(SERVER_ADDRESS, MC_SERVER_PORT)
        status = server.status()
        #print(f"Players online: {status.players.online}, Max players: {status.players.max}")
        return jsonify({
            "online_players": status.players.online,
            "max_players": status.players.max
        })
    except Exception as e:
        print(f"Error fetching player count: {e}")  # Log detailed error
        return jsonify({
            "error": "Could not retrieve player count",
            "details": str(e)
        }), 500



def get_active_players_using_mcstatus():
    """
    Uses mcstatus to fetch the currently active players on the Minecraft server.
    Returns a set of active player names or an error message if the server is unreachable.
    """
    try:
        # Query the Minecraft server
        server = JavaServer(SERVER_ADDRESS, MC_SERVER_PORT)
        status = server.status()

        # Extract the list of player names
        if status.players.sample:
            active_players = {player.name for player in status.players.sample}
        else:
            active_players = set()

        return active_players
    except Exception as e:
        print(f"Error querying server for active players: {e}")
        return set()

# Get Game Version
def get_minecraft_version():
    try:
        state = load_server_info()
        return state.get("server_version", "Unknown")
    except Exception:
        return "Unknown"

# Get online players!
@app.route('/online_players', methods=['GET'])
def online_players():
    """Fetch the list of online players and their pings."""
    try:
        # Parse active players using mcstatus
        active_players = get_active_players_using_mcstatus()
        #print(f"Active players: {active_players}")  # Debugging

        # Return the data in JSON format
        return jsonify({
            "players": sorted(list(active_players)),  # Sort for consistency
        })
    except Exception as e:
        error_message = f"Could not retrieve online players. Error: {e}"
        print(error_message)
        return jsonify({
            "error": "Could not retrieve online players",
            "details": error_message
        }), 500


























@app.route('/')
def index():
    minecraft_version = get_minecraft_version()
    players = load_data()
    return render_template('index.html', players=players, minecraft_version=minecraft_version, config=config_data)

# @app.route('/output_data')
# def output_data():
#     pass

@app.route('/player/<uuid>')
def player_page(uuid):
    players = load_data()
    player = next((p for p in players if p["uuid"] == uuid), None)
    if not player:
        return "Player not found", 404
    
    stats = None
    try:
        stats = load_player_stats(uuid)
    except FileNotFoundError:
        stats = None
        
    if stats is None:
        stats = {
            "minecraft:custom": {
                "minecraft:play_time": 100,
                "minecraft:jump": 0,
                "minecraft:walk_one_cm": 0,
                "minecraft:fly_one_cm": 0
            }
        }
        print(f"Stats file not found for {uuid}, using default.")
    
    return render_template('player.html', player=player, stats=stats, config=config_data)

@app.route('/player/<uuid>/advancements')
def player_advancements(uuid):
    players = load_data()
    player = next((p for p in players if p["uuid"] == uuid), None)
    if not player:
        return "Player not found", 404
    
    # Load the advancements for the player
    advancements = load_advancements(uuid) or {"multi_part_advancements": {}, "other_advancements": {}}
    
    return render_template('advancements.html', player=player, advancements=advancements, config=config_data)


if __name__ == "__main__":
    # Run initial data processing
    run_initial_processing()
    processing_thread = threading.Thread(target=data_loop)
    processing_thread.start()
    # Start the Flask app
    try:
        # Run the Flask app on the main thread
        print("Flask app starting...")
        print(f"Server is running on http://127.0.0.1:{FLASK_PORT} or http://{my_ip}:{FLASK_PORT}")
        app.run(debug=False, host=SERVER_HOST, port=FLASK_PORT)
    except KeyboardInterrupt:
        pass
        # Handle ^C (Ctrl+C) gracefully
    print("Received KeyboardInterrupt, stopping Flask app.")
    stop_event.set()  # Set the event to signal the thread to stop
    # Ensure the background thread finishes before the program exits
    processing_thread.join()
    print("Background processing complete, shutting down.")