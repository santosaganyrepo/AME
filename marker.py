import json
import os
import pandas as pd
from google import genai
from PIL import Image

# ==========================================
# 1. PASTE YOUR GEMINI API KEY BELOW
# ==========================================
API_KEY = "AIzaSyDTcwGZcA2HtDwEMzNn_MI00nAU0xqUVmI"
# ==========================================

client = genai.Client(api_key=API_KEY)

def process_exam_images():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    images_folder = os.path.join(script_dir, "IMAGES")
    
    if not os.path.exists(images_folder):
        print(f"❌ IMAGES folder not found at {images_folder}")
        return

    all_results = []
    valid_extensions = ('.png', '.jpg', '.jpeg', '.webp')
    
    print(f"Starting marking process for folder: {images_folder}")

    for filename in os.listdir(images_folder):
        if filename.lower().endswith(valid_extensions):
            print(f"--- Processing {filename} ---")
            image_path = os.path.join(images_folder, filename)
            img = Image.open(image_path)
            
            prompt = """
            You are an examiner. The image contains a printed question (item) and a handwritten answer.
            Return ONLY a JSON with:
            "item" (item name), "score" (numeric), "max_score" (numeric).
            """

            try:
                response = client.models.generate_content(
                    model="gemini-3-flash-preview",
                    contents=[prompt, img],
                    config={"response_mime_type": "application/json"}
                )
                result = json.loads(response.text)
                result["filename"] = filename
                all_results.append(result)
                print(f"✅ Finished marking {filename}")

            except Exception as e:
                print(f"❌ Failed to mark {filename}: {e}")

    # Save JSON results
    json_file = os.path.join(script_dir, "all_exam_results.json")
    with open(json_file, "w") as f:
        json.dump(all_results, f, indent=4)

    # --- Convert to clean grade sheet ---
    if all_results:
        # Assume all images are for one student for now
        student_name = "Student1"
        grade_dict = {}
        total_score = 0

        for r in all_results:
            item = r.get("item", "Unknown")
            score = r.get("score", 0)
            grade_dict[item] = score
            total_score += score

        # Add total
        grade_dict["Total"] = total_score

        # Convert to DataFrame
        df = pd.DataFrame([grade_dict], index=[student_name])
        df.index.name = "Name"

        # Save to ODS
        ods_file = os.path.join(script_dir, "results.ods")
        df.to_excel(ods_file, engine="odf")
        print(f"✅ Clean grade sheet saved to {ods_file}")

if __name__ == "__main__":
    if API_KEY == "PASTE_YOUR_GEMINI_API_KEY_HERE":
        print("❌ Paste your GEMINI API Key first.")
    else:
        process_exam_images()
