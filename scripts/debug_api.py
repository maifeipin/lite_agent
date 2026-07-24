import json
import time
import urllib.request
import urllib.parse
import sys
import os
import argparse
import sqlite3

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from core import config_loader

def send_chat_message(port, token, text):
    url = f"http://127.0.0.1:{port}/api/v1/chat"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}" if token else ""
    }
    data = json.dumps({
        "session_id": "debug_api_session",
        "text": text
    }).encode('utf-8')

    req = urllib.request.Request(url, data=data, headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        print(f"HTTP Error: {e.code} {e.reason}")
        sys.exit(1)
    except Exception as e:
        print(f"Error sending message: {e}")
        sys.exit(1)

def stream_task(port, token, task_id):
    url = f"http://127.0.0.1:{port}/api/v1/task/stream?task_id={task_id}&session_id=debug_api_session"
    headers = {
        "Authorization": f"Bearer {token}" if token else ""
    }
    req = urllib.request.Request(url, headers=headers)
    print(f"[*] Connected to stream for task_id={task_id}")
    try:
        with urllib.request.urlopen(req) as response:
            last_status = None
            finished_subtasks = set()
            for line in response:
                decoded_line = line.decode('utf-8').strip()
                if decoded_line.startswith('data: '):
                    data_str = decoded_line[6:]
                    if data_str == '[DONE]':
                        print("\n\n[*] Stream finished.")
                        break
                    try:
                        data = json.loads(data_str)
                        status = data.get('status')
                        msg = data.get('message')
                        
                        if msg and msg != last_status:
                            print(f"\n[*] {msg}", end='', flush=True)
                            last_status = msg
                        elif msg:
                            print(".", end='', flush=True)
                            
                        progress = data.get('progress')
                        if progress and isinstance(progress, dict) and 'subtasks' in progress:
                            for sub in progress['subtasks']:
                                sub_id = sub.get('id')
                                sub_status = sub.get('status')
                                if sub_status in ('done', 'failed') and sub_id not in finished_subtasks:
                                    finished_subtasks.add(sub_id)
                                    res = sub.get('result', '') or sub.get('error', '')
                                    print(f"\n[Subtask {sub_id}] {sub.get('name')} -> {sub_status.upper()}:\n{res}")
                        
                        if status in ('done', 'completed', 'failed', 'error'):
                            print(f"\n\n[*] Task {status.upper()}!")
                            break
                    except json.JSONDecodeError:
                        pass
    except Exception as e:
        print(f"\n[!] Stream error: {e}")

def query_recent_messages(limit=5):
    """
    Query the most recent messages from the sessions.db SQLite database.
    This is the best query for debugging agent conversation history.
    """
    db_path = os.path.join(project_root, 'data', 'sessions.db')
    if not os.path.exists(db_path):
        print(f"Database not found at {db_path}")
        return

    print(f"[*] Querying last {limit} messages from {db_path}...")
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # This query extracts the session, role, and a preview of the content
        query = '''
            SELECT session_key, role, 
                   substr(content, 1, 100) || CASE WHEN length(content) > 100 THEN '...' ELSE '' END as preview,
                   created_at
            FROM messages 
            ORDER BY id DESC 
            LIMIT ?;
        '''
        cursor.execute(query, (limit,))
        rows = cursor.fetchall()
        
        print("-" * 80)
        for row in reversed(rows):
            session_key, role, preview, created_at = row
            # Format timestamp
            time_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(created_at))
            print(f"[{time_str}] {session_key} | {role.upper()}:\n{preview}")
            print("-" * 80)
            
        conn.close()
    except Exception as e:
        print(f"Database query failed: {e}")

def main():
    parser = argparse.ArgumentParser(description="Debug API CLI for lite_agent")
    parser.add_argument('prompt', nargs='?', help='The prompt to send to the agent')
    parser.add_argument('--history', type=int, metavar='N', help='Query the last N messages from the database')
    args = parser.parse_args()

    if args.history:
        query_recent_messages(args.history)
        if not args.prompt:
            sys.exit(0)
            
    if not args.prompt:
        parser.print_help()
        sys.exit(1)
        
    config = config_loader.load_config()
    api_config = config.get('channels', {}).get('api', {})
    if not api_config.get('enabled'):
        print("API channel is not enabled in config.json")
        sys.exit(1)
        
    port = api_config.get('port', 8887)
    token = api_config.get('auth_token', '')
    
    print(f"[*] API Port: {port}")
    print(f"[*] Sending prompt: {args.prompt}")
    resp = send_chat_message(port, token, args.prompt)
    
    if resp.get('type') == 'sync':
        print(f"\n[*] Sync Response:\n{resp.get('response')}")
        if resp.get('logs'):
            print(f"\n[*] Sync Execution Logs ({len(resp['logs'])} items):")
            for line in resp['logs']:
                print(f"  {line}")
    elif resp.get('type') == 'async':
        task_id = resp.get('task_id')
        print(f"\n[*] Async Task created: {task_id}")
        stream_task(port, token, task_id)
    else:
        print(f"Unknown response: {resp}")

if __name__ == '__main__':
    main()
