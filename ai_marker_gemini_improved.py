"""
AI Marking Engine - Improved with Proper Scoring
Returns exact format: success message, total score, feedback, question scores
"""

from google import genai
from google.genai import types
import json
from pathlib import Path
from typing import Dict, List
import os
import time
import re

class AIMarker:
    def __init__(self, api_key: str = None):
        self.api_key = api_key or os.environ.get('GOOGLE_API_KEY')
        if not self.api_key:
            raise ValueError("GOOGLE_API_KEY not found.")
        
        # Initialize Client for V1 API
        self.client = genai.Client(
            api_key=self.api_key,
            http_options={'api_version': 'v1'}
        )
        
        # Use working model
        self.model_id = "models/gemini-2.5-flash"
        
        # Config for better responses
        self.generation_config = types.GenerateContentConfig(
            temperature=0.0,  # Slight creativity for feedback
            top_p=1.0,
            top_k=1,
            max_output_tokens=5096,
            candidate_count=1
        )

    def load_rubric_context(self, exam_path: Path) -> str:
        """Loads text rubric if it exists."""
        rubric_context = ""
        rubric_text_file = exam_path / "rubric.txt"
        if rubric_text_file.exists():
            with open(rubric_text_file, 'r') as f:
                rubric_context = f.read()
        return rubric_context

    def prepare_images_for_gemini(self, image_paths: List[Path]) -> List[types.Part]:
        """Converts image paths to Gemini API parts."""
        parts = []
        for img_path in image_paths:
            try:
                ext = img_path.suffix.lower()
                if ext == '.pdf':
                    print(f"⚠️  Skipping PDF: {img_path.name}")
                    continue
                if ext not in ['.jpg', '.jpeg', '.png', '.webp']:
                    continue
                
                with open(img_path, 'rb') as f:
                    image_data = f.read()
                
                mime_type = 'image/jpeg' if ext in ['.jpg', '.jpeg'] else f'image/{ext[1:]}'
                
                parts.append(types.Part.from_bytes(
                    data=image_data,
                    mime_type=mime_type
                ))
            except Exception as e:
                print(f"⚠️ Error loading image {img_path}: {e}")
        return parts

    def mark_student(self, student_id: str, student_name: str, exam_path: Path, student_folder: Path) -> Dict:
        """Marks a single student's work."""
        answer_pages = sorted(student_folder.glob("p*.jpg"))
        if not answer_pages:
            return {"success": False, "error": "No answer sheets found"}
        
        rubric_text = self.load_rubric_context(exam_path)
        
        # IMPROVED MARKING PROMPT - Very specific instructions
        marking_prompt = f"""You are an expert exam marker. Mark this student's exam carefully.

STUDENT: {student_name} (ID: {student_id})

INSTRUCTIONS:
1. First, check if the images show exam answers or unrelated content (selfies, animals, random photos)
2. If images are unrelated to exams, respond ONLY with: "Failed to mark!"
3. If images show exam answers, review the question paper and marking rubric provided
4. Mark each question the student attempted according to the rubric
5. Calculate the total percentage score

Base your marking 100% on the provided Ugandan New Curriculum rubric principles:
   - Knowledge & Conceptual Accuracy: Correct facts, definitions, scientific terms, logical sequence. 
   - Full marks for complete/accurate ideas; partial for vague/incomplete but identifiable correct points; no marks for wrong/irrelevant.
   - Application & Problem-Solving: Correct use of knowledge in the given scenario. Slightly credit logical application even if not perfect.
   - Communication: Clear structure, logical flow, readability. Penalize heavily only if disorganization seriously hinders understanding.
   - Reasoning: Cause-effect explanations, justification with evidence/concepts. Credit supported reasoning, not unsupported opinions.
   - Use positive marking: Award marks for what is correct/right. Give partial credit for every identifiable correct point or step.
   - Always refer to the question paper and rubric images provided for accurate marking. Dont base on your own creativity while ignoring 
    the questions and rubric, so always refer to the question , if there is no proper question paper and rubric provided, then dont do any
    marking, return "Failed to mark! (Im very strict on this)"
   - Each item is out of 20 marks.

RUBRIC/MARKING SCHEME:
{rubric_text if rubric_text else "Use the marking rubric shown in the images provided."}

Comapare the student's attempted questions or also known as items to the total 
questions required to be answered in the exam and see that, if the student has 
attempted more than is required, only mark up to the required number of questions 
as per the rubric and ignore the rest. And if the student has failed to attempt 
the required number of questions, only mark the ones they have attempted and penalize 
for not answering all.


OUTPUT FORMAT (MUST FOLLOW EXACTLY):

If content is valid exam answers, respond in this EXACT format:

Success!
TOTAL: [Calculated total percentage 0-100]

FEEDBACK: Item /Question number: [Score]/20, Item /Question number: [Score]/20, 
Item /Question number: [Score]/20, Item /Question number: [Score]/20, and so on 
for all the items or questions the student has answered and on the same feedback 
present a concise 5-7 line report on strengths, weaknesses, why the student failed and all areas for improvement.

CRITICAL RULES:
- Do NOT add percentages to individual items (e.g., use "Item 1: 15/20", not "Item 1: 75%").
- The "TOTAL" line must be the overall exam percentage.
- List ONLY items the student actually attempted.
- Do NOT use bolding (**) inside the breakdown list.

If content is NOT exam-related (selfies, animals, random images), respond ONLY:
Failed to mark!

CRITICAL RULES:
- TOTAL must be a number between 0-100
- Only list question numbers that the student actually attempted
- Scores should reflect actual performance based on rubric
- Be fair but accurate in scoring
- Feedback should be one sentence summarizing item or question breakdown, strengths, weaknesses and areas of improvement

Begin marking now:"""
        
        
        
        parts = [types.Part.from_text(text=marking_prompt)]
        
        # Load all materials
        print(f"📸 Loading materials...")
        
        # Question papers
        qp_parts = self.prepare_images_for_gemini(sorted(exam_path.glob("question_paper_*")))
        parts.extend(qp_parts)
        print(f"   Question papers: {len(qp_parts)}")
        
        # Rubrics
        rubric_parts = self.prepare_images_for_gemini(sorted(exam_path.glob("rubric_*")))
        parts.extend(rubric_parts)
        print(f"   Rubrics: {len(rubric_parts)}")
        
        # Student answers
        answer_parts = self.prepare_images_for_gemini(answer_pages)
        parts.extend(answer_parts)
        print(f"   Answer pages: {len(answer_parts)}")
        
        try:
            print(f"🤖 Marking student {student_id}...")
            
            response = self.client.models.generate_content(
                model=self.model_id,
                contents=parts,
                config=self.generation_config
            )
            
            # Get response text
            res_text = response.text.strip()
            print(f"📊 AI Response:\n{res_text}\n")
            
            # Check if marking failed
            if "Failed to mark!" in res_text or "failed to mark" in res_text.lower():
                print("❌ AI detected invalid content (not exam-related)")
                return {
                    "success": False,
                    "error": "Failed to mark! Images do not contain valid exam answers."
                }
            
            # Parse the response
            total_score = 0.0
            feedback = ""
            questions = {}
            
            # Extract TOTAL score first
            total_match = re.search(r'TOTAL:\s*(\d+\.?\d*)', res_text, re.IGNORECASE)
            if total_match:
                total_score = float(total_match.group(1))

            # Extract FEEDBACK section - everything after "FEEDBACK:" 
            feedback_match = re.search(r'FEEDBACK:\s*(.+)', res_text, re.IGNORECASE | re.DOTALL)
            if feedback_match:
                feedback = feedback_match.group(1).strip()
            else:
                feedback = ""

            # Extract question scores separately (they should NOT be part of feedback)
            question_pattern = re.compile(r'(Q|Item)\s*(\d+)\s*:\s*(\d+\.?\d*)/20', re.IGNORECASE)
            for match in question_pattern.finditer(res_text):
                q_num = match.group(2)
                q_score = float(match.group(3))
                questions[f"q{q_num}"] = {
                    "score": q_score,
                    "max_score": 20,
                    "feedback": ""
                }
            
            # Validate total score
            if total_score > 100:
                total_score = 100.0
            if total_score < 0:
                total_score = 0.0
            
            total_score = round(total_score, 1)
            
            # If no total score was extracted, try to calculate from questions
            if total_score == 0 and len(questions) > 0:
                avg_score = sum(q['score'] for q in questions.values()) / len(questions)
                total_score = round(avg_score, 1)
            
            # Check if we got valid data
            if total_score == 0 and len(questions) == 0:
                print("⚠️  Warning: No scores extracted")
                return {
                    "success": False,
                    "error": "AI did not return valid scores. Response: " + res_text[:200]
                }
            
            formatted_result = {
                "success": True,
                "student_id": student_id,
                "student_name": student_name,
                "total_score": total_score,
                "raw_score": 0,
                "max_score": 100,
                "status": "Marked",
                "questions": {},
                "overall_feedback": feedback if feedback else "Auto-marked by AI",
                "strengths": [],
                "areas_for_improvement": []
            }
            
            return formatted_result
            
        except Exception as e:
            print(f"❌ Error during marking: {str(e)}")
            import traceback
            traceback.print_exc()
            return {"success": False, "error": str(e)}

    def mark_all_students_in_session(self, exam_path: Path, delay: float = 15.0) -> Dict:
        """Batch marks all unmarked students."""
        metadata_file = exam_path / "students_metadata.json"
        if not metadata_file.exists():
            return {"success": False, "error": "No metadata found"}
        
        with open(metadata_file, 'r') as f:
            students_data = json.load(f)
        
        results = {"total": len(students_data), "marked": 0, "failed": 0, "details": []}
        
        for sid, info in students_data.items():
            if info.get("status") == "Marked":
                print(f"ℹ️  Student {sid} already marked, skipping...")
                results["marked"] += 1
                continue
            
            marking_res = self.mark_student(
                sid, 
                info.get("student_name", "Unknown"), 
                exam_path, 
                exam_path / f"student_{sid}"
            )
            
            if marking_res.get("success"):
                students_data[sid].update(marking_res)
                with open(metadata_file, 'w') as f:
                    json.dump(students_data, f, indent=4)
                results["marked"] += 1
                results["details"].append({
                    "student_id": sid,
                    "score": marking_res["total_score"],
                    "status": "success"
                })
                print(f"💾 Saved results for student {sid}")
            else:
                results["failed"] += 1
                results["details"].append({
                    "student_id": sid,
                    "error": marking_res.get("error"),
                    "status": "failed"
                })
            
            # Rate limiting
            if delay > 0:
                print(f"⏳ Waiting {delay}s before next student...")
                time.sleep(delay)
            
        return results

def mark_exam_session(exam_path: Path, api_key: str = None) -> Dict:
    return AIMarker(api_key=api_key).mark_all_students_in_session(exam_path)
















