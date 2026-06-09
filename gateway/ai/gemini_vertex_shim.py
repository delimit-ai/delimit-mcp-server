#!/usr/bin/env python3
import argparse
import sys
import os

try:
    from google import genai
except ImportError:
    print("Error: google-genai is not installed.")
    sys.exit(1)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-p", "--prompt", type=str)
    parser.add_argument("-m", "--model", type=str, default="gemini-3.1-pro-preview")
    parser.add_argument("-y", "--yolo", action="store_true")
    args = parser.parse_args()

    # Try AI Studio (non-Vertex) first, since 3.1 is in preview there.
    # It will automatically pick up GOOGLE_API_KEY from environment or ADC.
    try:
        # vertexai=False targets generativelanguage.googleapis.com
        client = genai.Client(vertexai=False)
        response = client.models.generate_content_stream(
            model=args.model,
            contents=args.prompt,
        )
        for chunk in response:
            if chunk.text:
                sys.stdout.write(chunk.text)
                sys.stdout.flush()
        print()
    except Exception as e:
        print(f"\n[AI Studio API Error] {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
