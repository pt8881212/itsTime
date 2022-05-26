# itsTime

## The Current State of Online Media Platforms

Social networks are an indispensable part of our society. Such networks are no longer simply a platform for delivering content; they monitor information and censor/flag messages that they deem inappropriate. When a user violates social media’s community guidelines, their content would be either deleted or heavily censored. Such violations may also result in either a permanent user ban or the user account will be temporarily suspended for a set period (1-6 months). In some cases, the user’s profile may be shadowbanned, preventing the content from gaining traction within the user’s network. 

## It's Time Enables Users to Monitor Content Bans

It’sTime allows users to check an account’s status for various subtle content bans such as deboosting, and typeahead restrictions. A user may request to fetch all their current public content for download. The website also presents various data stores such as a graph of connected users to a given account and their account status. 

Our tool collects all the required data using Twitter’s private frontend API. Given the volatile nature of such APIs, we provide a detailed guide to the process of discovering and effectively interfacing with private API endpoints. Overall, this project will serve as a data inspection tool and a blueprint for reverse engineering APIs. 

## Technologies used

This project was broken into three main components, a user-facing web frontend (JavaScript), a backend for processing user queries/API calls (Python), and an API testing setup (a hybrid of Python and JavaScript) 

## Future plans

This project can be further expanded by adding support for other platforms such as Facebook, Reddit, etc. The project may also require ongoing effort for the maintenance of our current data fetching mechanisms. As an ambitious goal, one may also explore the possibilities of tracking and automatically extracting valid API endpoints. 
