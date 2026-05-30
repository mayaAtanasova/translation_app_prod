## BACKEND APP
An application that should be able to communicate with all players - write to teh database, communicate with PI devices, assign user roles and manage credentials and assignments.
# Admin capabilities
An admin should be able to :
- assign user roles
- create, delete, edit events
- assign devices (RaspberryPis) to events
- assign languages to events
- generate passcodes for events
- distribute passcodes to users in bulk email
- match audio chunks - transcripts - translations - spoken output
- write and retrieve info to teh database

## DATABASE
Contains tables of data that is used by the app, incl:

# USERS
- name
- email

# ADMINS
- name
- email
- permissions (?)

# ROLES
- name

# EVENTS
- name
- date
- venue
- languages used
- device IDs of assigned devices
- users

# DEVICES
- Name/identification
- assigned language

# LANGUAGES
- name

# TRANSLATIONS
- event ID
- timestamp
- language

# TRANSCRIPTS
- event ID
- timestamp
- language

# SPOKEN TRANSLATION
- event ID
- timestamp
- Language

## UI application
A browser application that allows Admin and User interactions

# Amin section
- Requires login
- Has an "Events"tab - with a list of events and create button.  Each event can be edited/deleted
- "Create event" view
    * Input name, date, venue
    * Add users to event
    * Send out passcode (button)
    * Assign a device with respective language.  Dropdowns.
- "Edit/Delete event" view of an event allows to edit all of the above fields
- "Devices" view
    * opens device setup (event, language)
    * allows to access device to change settings

# Users section
- Landing page - Users should be shown a field where to input a passcode to take them to a specific event
- Event page - select a language; display translation, pley audio translation

