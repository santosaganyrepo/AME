document.addEventListener('DOMContentLoaded', function() {
    const uploadForm = document.getElementById('uploadForm');
    const fileInput = document.getElementById('files');
    const submitBtn = document.getElementById('submitBtn');
    const resultMessage = document.getElementById('resultMessage');
    
    uploadForm.addEventListener('submit', async function(e) {
        e.preventDefault();
        const formData = new FormData(uploadForm);
        submitBtn.disabled = true;
        submitBtn.textContent = 'Uploading...';
        
        try {
            const response = await fetch('/upload', {
                method: 'POST',
                body: formData
            });
            const result = await response.json();
            
            if (result.success) {
                resultMessage.innerHTML = '✅ Success! Uploaded ' + result.total_saved + ' file(s)';
                resultMessage.className = 'message success';
                resultMessage.style.display = 'block';
                setTimeout(() => { uploadForm.reset(); }, 2000);
            } else {
                resultMessage.innerHTML = '❌ Error: ' + result.message;
                resultMessage.className = 'message error';
                resultMessage.style.display = 'block';
            }
        } catch (error) {
            resultMessage.innerHTML = '❌ Network error. Please try again.';
            resultMessage.className = 'message error';
            resultMessage.style.display = 'block';
        }
        
        submitBtn.disabled = false;
        submitBtn.textContent = 'Upload Scripts';
    });
});
