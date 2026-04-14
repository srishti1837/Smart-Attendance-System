function captureAndSubmit() {
    if (navigator.geolocation) {
        navigator.geolocation.getCurrentPosition(function(position) {
            const lat = position.coords.latitude;
            const lng = position.coords.longitude;

            // Send to Flask via Fetch API
            fetch('/submit-attendance', {
                method: 'POST',
                headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                body: `lat=${lat}&lng=${lng}`
            })
            .then(response => response.text())
            .then(data => alert(data));
            
        }, function(error) {
            alert("Error: Please enable GPS/Location access.");
        });
    }
}