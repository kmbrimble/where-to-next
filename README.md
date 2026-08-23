# where-to-next
Trip itinerary tracker and manager

#Desired features
- mobile friendly
- front end hostable on github pages or html stored on mobile device/onedrive
- backend and user uploads stored on personal onedrive if possible so can be used offline
- take an excel/csv itinerary input as a database that can be updated and the app output updates live (or on manual refresh)
- location and date tracking so it know where in the itinerary you are
- displays time to leave, behind schedule, estaimted time to destination
- each destination is clickable that pulls up google maps driving directions (waze option preferable)
- clickable links to booking pdfs stored on onedrive
- can use google drive instead of onedrive if more flexible/reliable/easier to set up
- guest viewer version hosted on github pages - read only, lower detail version with broad location tracking ( show a location pin at an itinerary point, or at a "travelling" between two itinerary points, etc)
- ability to upload photos with comments against an itinerary location, have these stored on google/onedrive, so guests can view them. sort of a mini-blog but pinned to days and locations. editable by admin users only.
- guests be able to react to a photo or comment and add their own comments in response.
- adding accounts via an admin settings menu, choose admin or guest type account during creation
- itinerary is split by day. There is a home menu displayed on app startup or accessible by a sticky house button icon in the top left. The home menu shows each day as a selectable button, with the current day in a highlighted colour based on current date). The button text should be Day N (D MMM). There should be an All button that provides the full itinerary sectioned by day but all visible by srolling down. There should be a settings button (which is where the account creation for admin users foes and any other options)
- Ability to switch between dark and light mode
- user settings are remembered per user (not per device)
- long life access tokens (remember this device feature) as implemented in terriblebutler app issue #18 https://github.com/kmbrimble/terriblebutler/issues/18 via commit https://github.com/kmbrimble/terriblebutler/commit/a6bbba6
- ability to add location specific comments like price, opening times, snacks to bring, whatever (custom text field)
- check current traffic / eta for the next driving destination and adjust the leave time accordingly (do not use the "arrive by" feature as this regularly overstates the time required, only use the current time for current destination (not current location as user may be away from their vehicle) to next desination - if flagged as driving there. send mobile/browser notification to user if eta changes materially (> 10%) advising the suggested departure time to arrive at the next destiantion per the itinerary plan
- allow travel to select driving or walking (changes whether eta / departure time is calculated; walking directions should produce a google maps link set to walking mode)
- must work with apple carplay. i.e. the user can load the itinerary browser page on their mobile device and click a destination which then shows the driving directions on apple carplay
- there should be the ability to export the itinerary state including any document and image uploads, comments, reactions, etc to an archivable format at the user's request. Whether this is an itinerary.html with assets in a folder, a PDF document, Word document, or some other user-friendly visual format to allow viewing the mobile version offline in the future as a keepsake.
