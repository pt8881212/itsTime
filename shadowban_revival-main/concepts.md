# Key modifications to make this work 

One major bug with the original version was its lack of "loop" passing for the async actions. The web application was ran with a brand new eventloop instead of the original even loop used for "logging in" to the different account sessions. This was solved by passing the current event loop to the web app. 



