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
    
    prompt = f"""
    You are reformatting a work digest into a podcast script for text-to-speech.
    {language_instruction}
    
    CRITICAL RULES - DO NOT VIOLATE:
    1. Use ONLY information from the digest below. Do NOT add, infer, or assume ANY details.
    2. Do NOT mention specific dates, times, or events unless they are EXPLICITLY stated in the digest.
    3. Do NOT add transitional phrases that imply future actions (like "next Wednesday", "upcoming meeting", etc.) unless explicitly stated.
    4. If information is vague or unclear, keep it vague - do not clarify or expand on it.
    
    Formatting Guidelines:
    1. Start with: "Welcome back to your Daily Briefing. Here's what's happening today."
    2. Remove ALL "LINK" mentions - simply state the information without referencing links.
    3. Group related items naturally.
    4. Use a conversational, radio-friendly tone.
    5. No markdown formatting - plain text only.
    
    Here is the digest (use ONLY this information):
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

def generate_summary_from_digest(text):
    """Generate a one-sentence summary of the digest using Claude."""
    print("Generating summary for filename...")
    
    prompt = f"""Generate a brief, descriptive one-sentence summary (max 8 words) of this work digest for use in a filename.
    
Guidelines:
- Focus on the most important or interesting items
- Use concise, natural language
- No punctuation at the end
- Example: "WooCommerce builds and model migrations"
    
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
    """Runs claude CLI to get the digest text."""
    print("Generating digest text using Claude...")
    try:
        # Read the prompt from the plugin file
        prompt_path = os.path.expanduser("~/.claude/plugins/marketplaces/automattic-claude-code-plugins/plugins/context-a8c/commands/digest.md")

        if not os.path.exists(prompt_path):
            print(f"Error: Digest prompt file not found at {prompt_path}")
            sys.exit(1)

        with open(prompt_path, 'r') as f:
            prompt_content = f.read()

        print(f"Running Claude CLI with prompt from {prompt_path}")

        result = subprocess.run(
            ['claude', 'complete', '--dangerously-skip-permissions', '-p', prompt_content],
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

async def generate_audio(text, output_file="digest.mp3", voice_name="Enceladus"):
    """Generates audio from text using Google Gemini API and converts to MP3."""
    print(f"Generating audio with Gemini TTS (Voice: {voice_name})...")
    try:
        from google import genai
        from google.genai import types
        from pydub import AudioSegment
        
        client = genai.Client(api_key=GOOGLE_API_KEY)
        
        # Optimized prompt for breakfast television style
        # Using Gemini's style control capabilities for relaxed, natural delivery
        language_instruction = ""
        if SUMMARY_LANG != "en":
            language_instruction = f"Speak in {SUMMARY_LANG} language. "
        
        prompt = f"""{language_instruction}You're a friendly breakfast television host delivering the morning's updates. 

Speak at a relaxed, comfortable pace - slower than a news anchor, perfect for someone enjoying their morning coffee or breakfast. Use gentle, natural pauses between topics. Keep your tone warm, approachable, and conversational, like chatting with a friend over breakfast. Don't rush - this is morning television, not breaking news.

Think of this as a cozy morning show segment. Be engaging but unhurried.

{text}"""
        
        print(f"Sending request to Gemini TTS (Length: {len(prompt)} chars)...")
        
        # Use the Flash TTS model
        try:
            response = client.models.generate_content(
                model='gemini-2.5-flash-preview-tts',
                contents=prompt,
                config={
                    'response_modalities': ['AUDIO'],
                    'speech_config': {
                        'voice_config': {
                            'prebuilt_voice_config': {
                                'voice_name': voice_name
                            }
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
            
        # Extract audio data
        audio_data = None
        for part in response.parts:
            if part.inline_data and part.inline_data.data:
                audio_data = part.inline_data.data
                break
        
        if audio_data:
            # Convert raw PCM to MP3
            print("Converting raw PCM to MP3...")
            audio = AudioSegment(
                data=audio_data,
                sample_width=2,  # 16-bit
                frame_rate=24000,
                channels=1
            )
            audio.export(output_file, format="mp3")
            print(f"Audio saved to {output_file}")
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
    digest_file_path = f"/Users/wzieba/Automattic/Daily Digests/{today_str}.md"
    
    print(f"Checking for digest file at: {digest_file_path}")
    
    text = "" # Initialize text variable
    
    # Check for today's digest file
    if os.path.exists(digest_file_path):
        print(f"Digest file found at {digest_file_path}. Skipping generation.")
        with open(digest_file_path, 'r') as f:
            text = f.read()
    else:
        # Run Claude to generate the digest
        print("Running Claude to generate digest...")
        text = get_digest_text()
        
        # Save the generated text to the digest file
        if text:
            os.makedirs(os.path.dirname(digest_file_path), exist_ok=True)
            with open(digest_file_path, 'w') as f:
                f.write(text)
            print(f"Digest saved to {digest_file_path}")


            
    if not text:
        print("Error: Digest file is empty.")
        sys.exit(1)

    print(f"Digest text length: {len(text)} chars")
    
    # Rewrite digest with Claude (Podcast style)
    print("Rewriting digest for audio...")
    rewritten_text = rewrite_digest_with_claude(text, language_code=SUMMARY_LANG)
    print(f"Rewritten text length: {len(rewritten_text)} chars")
    
    # Extract title for filename (use original text for title extraction)
    title = generate_summary_from_digest(text)
    
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
    # Using Enceladus - optimized for morning coffee listening
    await generate_audio(rewritten_text, audio_file, voice_name="Enceladus")
    
    # 3. Upload
    if os.path.exists(audio_file):
        await upload_to_pocket_casts(audio_file)
    else:
        print("Audio file not found, skipping upload.")

if __name__ == "__main__":
    asyncio.run(main())
