import asyncio
import os
import sys

# Ensure project root is in sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.tasks import author_check, _temp_author_fetch_for_recovery

async def run_script():
    file_path = '/home/invocation/copixiv/author_ids.txt'
    
    with open(file_path, 'r') as f:
        lines = f.readlines()

    author_ids = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith('#'):
            author_ids.append(stripped)

    print(f"Loaded {len(author_ids)} author IDs to check.")

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
            
        try:
            aid = int(stripped)
        except ValueError:
            print(f"Invalid author ID: {stripped}")
            continue

        print(f"Executing task for author ID: {aid}")
        success = False
        try:
            # Execute within the single event loop so global PixivClient tasks 
            # aren't created in closed loops.
            await _temp_author_fetch_for_recovery(author_id=aid)
            await author_check(author_id=aid)
            print(f"Successfully finished author {aid}")
            success = True
        except Exception as e:
            print(f"Error checking author {aid}: {e}")
        
        # Only comment out if it was successful, otherwise leave it uncommented for retry next time
        if success:
            lines[i] = f"# {line}"
            with open(file_path, 'w') as f:
                f.writelines(lines)
            print(f"Marked author {aid} as completed.")
        else:
            print(f"Author {aid} failed, skipping mark as completed to retry on next run.")

    print("All tasks completed.")

if __name__ == "__main__":
    asyncio.run(run_script())
