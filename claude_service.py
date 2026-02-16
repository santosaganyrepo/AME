openai_key = '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'


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
TOTAL: [percentage score 0-100]
FEEDBACK: Give a credible and short report(probably 4 to 5 lines) regarding 
the student's strengths, weaknesses and areas of improvement.

List the question or item numbers attempted by the student with their individual scores.(Each item is out of 20)

(Only list questions the student actually answered)

If content is NOT exam-related (selfies, animals, random images), respond ONLY:
Failed to mark!

CRITICAL RULES:
- TOTAL must be a number between 0-100
- Only list question numbers that the student actually attempted
- Scores should reflect actual performance based on rubric
- Be fair but accurate in scoring
- Feedback should be one sentence summarizing strengths, weaknesses and areas of improvement

Begin marking now:"""