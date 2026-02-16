
openai_key = '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'


import base64
import re
from pathlib import Path
from openai import OpenAI
from typing import Dict, List

class OpenAIMarker:
    def __init__(self, api_key: str = None):
        # HARDCODED KEY: Replace the string below with your actual sk-... key
        self.api_key = "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"
        self.client = OpenAI(api_key=self.api_key)
        self.model = "gpt-4o"

    def encode_image(self, image_path: Path):
        with open(image_path, "rb") as image_file:
            return base64.b64encode(image_file.read()).decode('utf-8')

    def mark_student(self, student_id: str, student_name: str, exam_path: Path, student_folder: Path) -> Dict:
        """Marks using GPT-4o with Base64 images."""
        answer_pages = sorted(student_folder.glob("p*.jpg"))
        if not answer_pages:
            return {"success": False, "error": "No answer sheets found"}
        
        # Build prompt
        prompt_text = "Mark this exam strictly. Output exactly in this format: 'TOTAL: [score]' followed by 'Q[num]: [score]'. No other text."
        content = [{"type": "text", "text": prompt_text}]
        
        # Add Images (Question Papers + Answers)
        # Note: Rubrics must be JPG/PNG for OpenAI
        all_images = sorted(exam_path.glob("question_paper_*")) + sorted(exam_path.glob("rubric_*")) + answer_pages
        
        for img in all_images:
            if img.suffix.lower() in ['.jpg', '.jpeg', '.png']:
                content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{self.encode_image(img)}", "detail": "high"}
                })

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": content}],
                temperature=0.0
            )
            res_text = response.choices[0].message.content
            
            # --- PARSING LOGIC (Matches your current system) ---
            total_score = 0.0
            questions = {}
            lines = res_text.split('\n')
            for line in lines:
                line = line.strip()
                if not line: continue
                
                if line.upper().startswith('TOTAL:'):
                    match = re.search(r'TOTAL:\s*(\d+\.?\d*)', line, re.IGNORECASE)
                    if match: total_score = float(match.group(1))
                else:
                    # Matches Q1: 10 or Item 1: 10
                    match = re.search(r'(?:Q|Item|item)\s*(\d+):\s*(\d+\.?\d*)', line, re.IGNORECASE)
                    if match:
                        q_num = match.group(1)
                        questions[f"q{q_num}"] = {"score": float(match.group(2)), "max_score": 100}

            return {
                "success": True,
                "student_id": student_id,
                "student_name": student_name,
                "total_score": total_score,
                "questions": questions,
                "overall_feedback": "Marked by GPT-4o"
            }
        except Exception as e:
            return {"success": False, "error": str(e)}