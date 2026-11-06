# Browser connector

This development extension supports Chrome and Chromium browsers. Extract the ZIP, open the browser's extension manager, enable Developer mode, and choose Load unpacked. Select this folder. It has not been published to an extension store, so this step cannot yet be replaced with a normal store installation.

Open the extension from an ordinary website tab. Enter your HiveMind server address and a one-time pairing code from the dashboard. The browser asks permission to connect to that server. Click Share this tab if you want HiveMind to read its text. Approve the exact action in the dashboard.

Keep the popup open while receiving a task. Closing it pauses polling. This deliberate first version avoids claiming that a sleeping Manifest V3 worker is an always-on browser agent. A tab must be shared again after navigating to another site. browser.read returns up to 16,000 characters of visible text, its URL, and title. browser.open creates an inactive tab and does not automatically grant read access to it.

Only browser.read and browser.open are implemented. There is no clicking, typing, form submission, cookie access, or arbitrary script tool. A durable task journal prevents replay after a lost acknowledgment. A crash between execution and reporting leaves an uncertain outcome for the user to inspect.

Device tokens stay in extension local storage. Revoke the browser in the dashboard to invalidate the token. Disconnect locally removes it from this browser but does not itself revoke the server credential.
