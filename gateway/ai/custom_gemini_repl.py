#!/usr/bin/env python3
import sys
import os
import argparse
import readline  # Enables history and arrow keys

try:
    from google import genai
except ImportError:
    print("Error: google-genai is not installed.")
    sys.exit(1)

def print_banner():
    print("\033[35m\033[1m    ____  ________    ______  _____________\033[0m")
    print("\033[35m\033[1m   / __ \/ ____/ /   /  _/  |/  /  _/_  __/\033[0m")
    print("\033[91m\033[1m  / / / / __/ / /    / // /|_/ // /  / /   \033[0m")
    print("\033[91m\033[1m / /_/ / /___/ /____/ // /  / // /  / /    \033[0m")
    print("\033[33m\033[1m/_____/_____/_____/___/_/  /_/___/ /_/     \033[0m")
    print("  \033[2mNative Vertex AI Edition\033[0m\n")

def main():
    parser = argparse.ArgumentParser(description="Custom Gemini Vertex REPL")
    parser.add_argument("-p", "--prompt", type=str, help="Initial prompt")
    parser.add_argument("-m", "--model", type=str, default="gemini-3.1-pro-preview", help="Model name")
    parser.add_argument("-y", "--yolo", action="store_true", help="YOLO mode")
    args = parser.parse_args()

    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "jamsons")
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
    
    try:
        client = genai.Client(vertexai=True, project=project, location=location)
        chat = client.chats.create(model=args.model)
    except Exception as e:
        print(f"\n[Vertex API Initialization Error] {e}", file=sys.stderr)
        sys.exit(1)

    if not os.environ.get("DELIMIT_QUIET") == "true":
        print_banner()

    # If an initial prompt was provided (e.g. from Auto-Phoenix), execute it and return
    if args.prompt:
        try:
            response = chat.send_message_stream(args.prompt)
            for chunk in response:
                if chunk.text:
                    sys.stdout.write(chunk.text)
                    sys.stdout.flush()
            print()
            sys.exit(0)
        except Exception as e:
            print(f"\n[Vertex API Error] {e}", file=sys.stderr)
            sys.exit(1)

    # Interactive Loop
    while True:
        try:
            user_input = input("\033[36mgemini>\033[0m ")
            if not user_input.strip():
                continue
            if user_input.strip() in ("/exit", "/quit"):
                break
                
            response = chat.send_message_stream(user_input)
            for chunk in response:
                if chunk.text:
                    sys.stdout.write(chunk.text)
                    sys.stdout.flush()
            print()
            
        except EOFError:
            break
        except KeyboardInterrupt:
            print("\n(Ctrl+C) Type /exit to quit.")
        except Exception as e:
            print(f"\n[Vertex API Error] {e}", file=sys.stderr)
            break

if __name__ == "__main__":
    main()
