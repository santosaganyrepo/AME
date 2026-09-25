// Upload functionality for exam scripts

document.addEventListener('DOMContentLoaded', function() {
    const uploadForm = document.getElementById('uploadForm');
    const fileInput = document.getElementById('files');
    const fileList = document.getElementById('fileList');
    const progressBar = document.getElementById('progressBar');
    const progressFill = document.getElementById('progressFill');
    const progressText = document.getElementById('progressText');
    const resultMessage = document.getElementById('resultMessage');
    const submitBtn = document.getElementById('submitBtn');
    
    let selectedFiles = [];
    
    // Handle file selection
    fileInput.addEventListener('change', function(e) {
        selectedFiles = Array.from(e.target.files);
        displayFileList();
    });
    
    // Display selected files
    function displayFileList() {
        fileList.innerHTML = '';
        
        if (selectedFiles.length === 0) {
            return;
        }
        
        selectedFiles.forEach((file, index) => {
            const fileItem = document.createElement('div');
            fileItem.className = 'file-item';
            fileItem.innerHTML = `
                <span>📄 ${file.name} (${formatFileSize(file.size)})</span>
                <button type="button" onclick="removeFile(${index})">Remove</button>
            `;
            fileList.appendChild(fileItem);
        });
    }
    
    // Remove file from selection
    window.removeFile = function(index) {
        selectedFiles.splice(index, 1);
        
        // Update file input
        const dt = new DataTransfer();
        selectedFiles.forEach(file => dt.items.add(file));
        fileInput.files = dt.files;
        
        displayFileList();
    };
    
    // Format file size
    function formatFileSize(bytes) {
        if (bytes === 0) return '0 Bytes';
        const k = 1024;
        const sizes = ['Bytes', 'KB', 'MB', 'GB'];
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        return Math.round(bytes / Math.pow(k, i) * 100) / 100 + ' ' + sizes[i];
    }
    
    // Handle form submission
    uploadForm.addEventListener('submit', async function(e) {
        e.preventDefault();
        
        // Validate files
        if (selectedFiles.length === 0) {
            showMessage('Please select at least one file', 'error');
            return;
        }
        
        // Prepare form data
        const formData = new FormData(uploadForm);
        
        // Clear old files and add new ones
        formData.delete('files[]');
        selectedFiles.forEach(file => {
            formData.append('files[]', file);
        });
        
        // Disable submit button
        submitBtn.disabled = true;
        submitBtn.textContent = 'Uploading...';
        
        // Show progress bar
        progressBar.style.display = 'block';
        resultMessage.style.display = 'none';
        
        try {
            const response = await fetch('/upload', {
                method: 'POST',
                body: formData
            });
            
            const result = await response.json();
            
            // Update progress to 100%
            updateProgress(100);
            
            if (result.success) {
                showMessage(
                    `✅ Success! Uploaded ${result.total_saved} file(s) for student ${result.student_id}`,
                    'success'
                );
                
                // Show details
                if (result.failed_files && result.failed_files.length > 0) {
                    const failedList = result.failed_files
                        .map(f => `• ${f.filename}: ${f.error}`)
                        .join('<br>');
                    showMessage(
                        `⚠️ Some files failed:<br>${failedList}`,
                        'error'
                    );
                }
                
                // Reset form after 2 seconds
                setTimeout(() => {
                    uploadForm.reset();
                    selectedFiles = [];
                    displayFileList();
                    progressBar.style.display = 'none';
                    submitBtn.disabled = false;
                    submitBtn.textContent = 'Upload Scripts';
                }, 2000);
                
            } else {
                showMessage(`❌ Error: ${result.message}`, 'error');
                submitBtn.disabled = false;
                submitBtn.textContent = 'Upload Scripts';
            }
            
        } catch (error) {
            console.error('Upload error:', error);
            showMessage('❌ Network error. Please try again.', 'error');
            submitBtn.disabled = false;
            submitBtn.textContent = 'Upload Scripts';
        }
    });
    
    // Update progress bar
    function updateProgress(percent) {
        progressFill.style.width = percent + '%';
        progressText.textContent = percent + '%';
    }
    
    // Show message
    function showMessage(message, type) {
        resultMessage.innerHTML = message;
        resultMessage.className = `message ${type}`;
        resultMessage.style.display = 'block';
        
        // Auto-hide success messages
        if (type === 'success') {
            setTimeout(() => {
                resultMessage.style.display = 'none';
            }, 5000);
        }
    }
    
    // Drag and drop functionality
    const uploadBox = document.querySelector('.file-upload-box');
    
    uploadBox.addEventListener('dragover', function(e) {
        e.preventDefault();
        uploadBox.style.background = '#e8eaff';
        uploadBox.style.borderColor = '#764ba2';
    });
    
    uploadBox.addEventListener('dragleave', function(e) {
        e.preventDefault();
        uploadBox.style.background = '#f8f9ff';
        uploadBox.style.borderColor = '#667eea';
    });
    
    uploadBox.addEventListener('drop', function(e) {
        e.preventDefault();
        uploadBox.style.background = '#f8f9ff';
        uploadBox.style.borderColor = '#667eea';
        
        const files = Array.from(e.dataTransfer.files);
        selectedFiles = files;
        
        // Update file input
        const dt = new DataTransfer();
        files.forEach(file => dt.items.add(file));
        fileInput.files = dt.files;
        
        displayFileList();
    });
    
    // Auto-generate student ID based on class
    const classSelect = document.getElementById('class');
    const studentIdInput = document.getElementById('student_id');
    
    classSelect.addEventListener('change', function() {
        if (classSelect.value && !studentIdInput.value) {
            // Generate a sample ID
            const timestamp = Date.now().toString().slice(-4);
            studentIdInput.value = `${classSelect.value}-${timestamp}`;
        }
    });
});