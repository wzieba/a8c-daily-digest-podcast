import os
import sys
import asyncio
import subprocess
import re
from datetime import datetime
from dotenv import load_dotenv
from google import genai
from playwright.async_api import async_playwright

# Load environment variables
load_dotenv()

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
SUMMARY_LANG = os.getenv("SUMMARY_LANG", "en")  # Default to English
TTS_MODEL = os.getenv("TTS_MODEL", "gemini-2.5-flash-preview-tts")  # Default to flash (free tier)
POCKET_CASTS_EMAIL = os.getenv("POCKET_CASTS_EMAIL")
POCKET_CASTS_PASSWORD = os.getenv("POCKET_CASTS_PASSWORD")

if not GOOGLE_API_KEY:
    print("Error: GOOGLE_API_KEY not found in environment variables.")
    sys.exit(1)

if not POCKET_CASTS_EMAIL or not POCKET_CASTS_PASSWORD:
    print("Error: POCKET_CASTS_EMAIL or POCKET_CASTS_PASSWORD not found in environment variables.")
    sys.exit(1)

def clean_markdown_for_tts(text):
    """Remove markdown links and other formatting that doesn't work well with TTS."""
    # Remove markdown links but keep the link text
    # Pattern: [text](url) -> text
    text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', text)
    
    # Remove standalone URLs
    text = re.sub(r'https?://\S+', '', text)
    
    # Remove markdown bold/italic markers
    text = re.sub(r'\*\*([^\*]+)\*\*', r'\1', text)
    text = re.sub(r'\*([^\*]+)\*', r'\1', text)
    
    # Remove markdown headers (keep the text)
    text = re.sub(r'^#+\s+', '', text, flags=re.MULTILINE)
    
    return text.strip()

def rewrite_digest_with_claude(text, language_code="en"):
    """Rewrites the digest text using Claude to be more conversational and remove links."""
    # Check if rewritten digest already exists for today
    rewritten_dir = os.path.join(os.path.dirname(__file__), 'rewritten_digests')
    date_str = datetime.now().strftime("%Y-%m-%d")
    rewritten_file = os.path.join(rewritten_dir, f'{date_str}.txt')
    
    if os.path.exists(rewritten_file):
        print(f"Rewritten digest already exists: {rewritten_file}")
        with open(rewritten_file, 'r', encoding='utf-8') as f:
            return f.read()
    
    print("Rewriting digest with Claude (Podcast style)...")
    
    language_instruction = ""
    if language_code != "en":
        language_instruction = f"\n    IMPORTANT: Write the entire script in {language_code} language."
    
    prompt = f"""You are writing a podcast script based on a work digest. 
    There are two hosts:
    1. Sarah (Female): Enthusiastic, leads the conversation, introduces topics.
    2. Mike (Male): Analytical, adds depth, asks clarifying questions or provides details.

    {language_instruction}
    
    CRITICAL RULES:
    1. Use ONLY information from the digest.
    2. Format the output as a script:
       Sarah: [Text]
       Mike: [Text]
    3. Keep it conversational and natural.
    4. Start with a catchy intro.
    5. Do NOT mention specific dates unless explicitly stated.
    6. Output ONLY the script. Do NOT add any commentary, explanations, questions, or meta-text before or after the script.
    7. Start your response directly with "Sarah:" - no preamble.
    
    Digest:
    {text}
    """
    
    try:
        # Run claude in print mode
        result = subprocess.run(
            ['claude', '-p', prompt],
            capture_output=True,
            text=True,
            check=True
        )
        rewritten = result.stdout.strip()
        
        # Save rewritten text to dated file in rewritten_digests directory
        os.makedirs(rewritten_dir, exist_ok=True)
        
        with open(rewritten_file, 'w', encoding='utf-8') as f:
            f.write(rewritten)
        print(f"Rewritten text saved to: {rewritten_file}")
        
        return rewritten
    except subprocess.CalledProcessError as e:
        print(f"Error rewriting with Claude: {e}")
        print(f"Stderr: {e.stderr}")
        # Fallback to original text if rewrite fails
        return clean_markdown_for_tts(text)
    except FileNotFoundError:
        print("Error: 'claude' CLI not found.")
        return clean_markdown_for_tts(text)

def generate_summary_from_digest(text, language_code="en"):
    """Generate a one-sentence summary of the digest using Claude."""
    print("Generating summary for filename...")
    
    language_instruction = ""
    if language_code != "en":
        language_instruction = f"\n- Write the summary in {language_code} language."
    
    prompt = f"""Generate a brief, descriptive one-sentence summary (max 8 words) of this work digest for use in a filename.
    
Guidelines:
- Focus on the most important or interesting items
- Use concise, natural language
- No punctuation at the end
- Output ONLY the summary, nothing else
- Example: "WooCommerce builds and model migrations"{language_instruction}
    
Digest:
{text[:2000]}
"""
    
    try:
        result = subprocess.run(
            ['claude', '-p', prompt],
            capture_output=True,
            text=True,
            check=True,
            timeout=30
        )
        summary = result.stdout.strip()
        # Clean up any quotes or punctuation
        summary = summary.strip('"\'').rstrip('.!?')
        # Limit length
        if len(summary) > 60:
            summary = summary[:57] + '...'
        return summary
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"Error generating summary: {e}")
        # Fallback to simple extraction
        return "Work Digest"

def get_digest_text():
    """Runs claude CLI with the digest slash command to get the digest text."""
    print("Generating digest text using Claude /digest command...")
    try:
        # Plugin directory for the context-a8c plugin
        plugin_dir = os.path.expanduser("~/.claude/plugins/marketplaces/automattic-claude-code-plugins/plugins/context-a8c")

        if not os.path.exists(plugin_dir):
            print(f"Error: Plugin directory not found at {plugin_dir}")
            sys.exit(1)

        print(f"Running Claude CLI with plugin from {plugin_dir}")

        # Use the slash command with plugin-dir flag
        # Add instructions to skip interactive questions and auto-save
        result = subprocess.run(
            [
                'claude', '-p',
                '--plugin-dir', plugin_dir,
                '--dangerously-skip-permissions',
                '--', '/context-a8c:digest Generate a new digest for today, do not ask any questions, automatically save to the default location'
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=600  # 10 minute timeout
        )
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        print("Warning: Claude digest generation timed out after 10 minutes.")
        return ""
    except subprocess.CalledProcessError as e:
        print(f"Error running claude: {e}")
        print(f"Stderr: {e.stderr}")
        sys.exit(1)
    except FileNotFoundError:
        print("Error: 'claude' CLI not found.")
        sys.exit(1)

def parse_script_to_turns(script):
    """Parse a script with 'Speaker: text' format into structured turns."""
    turns = []
    lines = script.strip().split('\n')

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # Match pattern "Speaker: text"
        match = re.match(r'^([A-Za-z0-9]+):\s*(.+)$', line)
        if match:
            speaker, text = match.groups()
            turns.append({
                'speaker': speaker,
                'text': text.strip()
            })
        else:
            # If line doesn't match pattern, append to last turn if exists
            if turns:
                turns[-1]['text'] += ' ' + line

    return turns

async def generate_audio(text, output_file="digest.mp3"):
    """Generates audio from text using Google Gemini API with multi-speaker support."""
    print(f"Generating audio with Gemini multi-speaker TTS (Model: {TTS_MODEL})...")
    print("  Female voice (Sarah): Callirrhoe")
    print("  Male voice (Mike): Charon")

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=GOOGLE_API_KEY)

        # Optimized prompt for 2-speaker podcast
        language_instruction = ""
        if SUMMARY_LANG != "en":
            language_instruction = f"Speak in {SUMMARY_LANG} language. "

        prompt = f"""{language_instruction}TTS the following conversation between Sarah and Mike:
{text}"""

        print(f"Sending request to Gemini TTS (Length: {len(prompt)} chars)...")

        # Use the configured Gemini TTS model with multi-speaker support
        try:
            response = client.models.generate_content(
                model=TTS_MODEL,
                contents=prompt,
                config={
                    'response_modalities': ['AUDIO'],
                    'speech_config': {
                        'multi_speaker_voice_config': {
                            'speaker_voice_configs': [
                                {
                                    'speaker': 'Sarah',
                                    'voice_config': {
                                        'prebuilt_voice_config': {
                                            'voice_name': 'Charon'  # Swapped: flash model reverses the assignments
                                        }
                                    }
                                },
                                {
                                    'speaker': 'Mike',
                                    'voice_config': {
                                        'prebuilt_voice_config': {
                                            'voice_name': 'Callirrhoe'  # Swapped: flash model reverses the assignments
                                        }
                                    }
                                }
                            ]
                        }
                    }
                }
            )
        except Exception as api_error:
            print(f"CRITICAL ERROR calling Gemini API: {api_error}")
            # Check for common errors
            if "429" in str(api_error):
                print("Tip: You might have hit a rate limit or quota.")
            elif "400" in str(api_error):
                print("Tip: The request might be invalid or too long.")
            raise api_error

        # Check if response has parts
        if not response.parts:
            print("Error: No content generated in response.")
            print(f"Response feedback: {response.prompt_feedback}")
            sys.exit(1)

        # Extract audio data (PCM format)
        audio_data = None
        for part in response.parts:
            if part.inline_data and part.inline_data.data:
                audio_data = part.inline_data.data
                break

        if audio_data:
            # Save PCM data to temporary file
            pcm_file = output_file.replace('.mp3', '.pcm')
            print(f"Saving PCM audio to {pcm_file}...")
            with open(pcm_file, "wb") as f:
                f.write(audio_data)

            # Convert PCM to MP3 using ffmpeg for better quality
            print(f"Converting to MP3 with ffmpeg...")
            result = subprocess.run(
                ['ffmpeg', '-f', 's16le', '-ar', '24000', '-ac', '1', '-i', pcm_file, '-y', output_file],
                capture_output=True,
                text=True
            )

            if result.returncode == 0:
                print(f"Audio saved to {output_file}")
                # Clean up PCM file
                os.remove(pcm_file)
            else:
                print(f"Error converting with ffmpeg: {result.stderr}")
                print(f"PCM file saved at: {pcm_file}")
        else:
            print("Error: No audio data found in response.")
            print(response)
            sys.exit(1)

    except Exception as e:
        print(f"Error generating audio: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

async def upload_to_pocket_casts(file_path):
    """Uploads the audio file to Pocket Casts."""
    print("Uploading to Pocket Casts...")
    async with async_playwright() as p:
        # Launch browser (headless=True for production, False for debug)
        browser = await p.chromium.launch(headless=False) 
        context = await browser.new_context()
        page = await context.new_page()

        try:
            print("Logging in...")
            await page.goto("https://play.pocketcasts.com/user/login")
            
            # Login
            await page.fill('input[name="email"]', POCKET_CASTS_EMAIL)
            await page.fill('input[name="password"]', POCKET_CASTS_PASSWORD)
            await page.click('button[type="submit"]')
            
            # Wait for login to complete
            await page.wait_for_url("**/podcasts", timeout=15000)
            print("Logged in.")
            
            # Navigate to Files
            print("Navigating to Files...")
            await page.goto("https://pocketcasts.com/uploaded-files")
            await page.wait_for_load_state("networkidle")
            
            # Click "Upload New" button
            print("Clicking 'Upload New'...")
            await page.click('text="Upload New"')
            await page.wait_for_timeout(1000)
            
            
            # Upload file (input is hidden, so don't wait for visibility)
            print("Uploading file...")
            await page.set_input_files('input[type="file"]', file_path)
            print("File selected")

            # Wait for upload to complete
            print("Waiting for upload to complete...")
            await page.wait_for_timeout(10000)
            
            print("Upload process finished.")

        except Exception as e:
            print(f"Error uploading to Pocket Casts: {e}")
            import traceback
            traceback.print_exc()
        finally:
            await browser.close()

async def main():
    # 1. Get Digest Text
    # Instead of parsing CLI output, we read the generated file
    today_str = datetime.now().strftime("%Y-%m-%d")
    digest_file_path = os.path.expanduser(f"~/Automattic/Daily Digests/{today_str}.md")
    
    print(f"Checking for digest file at: {digest_file_path}")
    
    text = "" # Initialize text variable
    
    # Check for today's digest file
    if os.path.exists(digest_file_path):
        print(f"Digest file found at {digest_file_path}. Skipping generation.")
        with open(digest_file_path, 'r') as f:
            text = f.read()
    else:
        # Run Claude to generate the digest
        # The slash command auto-saves to the digest file, so we just need to run it
        # and then read the file it creates (don't use stdout - that's just a summary)
        print("Running Claude to generate digest...")
        get_digest_text()  # This triggers the slash command which auto-saves
        
        # Now read the file that Claude created
        if os.path.exists(digest_file_path):
            with open(digest_file_path, 'r') as f:
                text = f.read()
            print(f"Digest loaded from {digest_file_path}")
        else:
            print(f"Error: Claude did not create digest file at {digest_file_path}")
            sys.exit(1)


            
    if not text:
        print("Error: Digest file is empty.")
        sys.exit(1)

    print(f"Digest text length: {len(text)} chars")
    
    # Rewrite digest with Claude (Podcast style)
    print("Rewriting digest for audio...")
    rewritten_text = rewrite_digest_with_claude(text, language_code=SUMMARY_LANG)
    print(f"Rewritten text length: {len(rewritten_text)} chars")
    
    # Extract title for filename (use original text for title extraction)
    title = generate_summary_from_digest(text, language_code=SUMMARY_LANG)
    
    # Generate filename with date and summary
    date_str = datetime.now().strftime("%d %b %Y")
    # Sanitize title for filename (keep spaces, remove special chars)
    safe_title = re.sub(r'[^\w\s-]', '', title).strip()
    
    # Ensure generated_audio directory exists
    output_dir = os.path.join(os.path.dirname(__file__), 'generated_audio')
    os.makedirs(output_dir, exist_ok=True)
    
    audio_file = os.path.join(output_dir, f"{date_str} - {safe_title}.mp3")
    print(f"Output filename: {audio_file}")
    
    # 2. Generate Audio
    # Using Gemini 2.0 Flash Exp for multi-speaker podcast
    await generate_audio(rewritten_text, audio_file)
    
    # 3. Upload
    if os.path.exists(audio_file):
        await upload_to_pocket_casts(audio_file)
    else:
        print("Audio file not found, skipping upload.")

if __name__ == "__main__":
    asyncio.run(main())
