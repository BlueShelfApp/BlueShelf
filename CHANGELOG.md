# Changelog

## Unreleased

**Passthrough copies**

- **Tools → Make a passthrough copy** cuts the performer out of a VR scene and packs the mask into
  the corners in the layout DeoVR reads, so a scene that was never shot for passthrough plays
  against your own room. The matting is done by VRAutoMatte, which BlueShelf finds and runs; it
  stays a separate download because it carries PyTorch and a CUDA build with it.
- The lens angle and projection are read from the file, so a copy of a 190° fisheye is named and
  played back as one.
- Several videos at once: **Add from library…** picks them by performer, studio, category, folder
  or any of the sidebar's views, **Browse for files…** picks them off disk, and the list runs one
  after another. A video that fails does not stop the rest.
- Finished copies are added to the library and marked **Passthrough**.
- **Skipping the talking.** A cheap scan finds the stretches with action in them and mattes only
  those; the rest becomes a blank mask, so the scene plays normally through the conversation and
  the room appears when it starts. The video is not cut and stays its original length. The scan is
  drawn on an editable strip, and the threshold is taken from each video's own spread rather than
  being fixed.
- The scan judges **by what is exposed** where the NudeNet detector is installed, and falls back to
  counting skin-coloured pixels where it is not. Skin turned out to be a poor guide in VR: on a
  real scene it called the last four minutes quiet when they were the opposite. The detector put
  the same file at one stretch covering 90% of it, matching what the viewer said.
- New **Detail** setting deciding how much of each frame the matting model sees, defaulting to a
  level well above what VRAutoMatte would have picked. Its table drops an 8 GB card to nearly its
  coarsest setting; on a real scene that left soft edges and green showing through the performer.
  The same card handled four times the pixels at 17% more time.
- Fixed: a helper could not file a finished chunk when the shared folder was on a different
  drive from its scratch space — which is the normal arrangement, since chunks are built on local
  disk on purpose and the shared folder is on the network. Every chunk failed with "cannot move
  the file to a different disk drive".
- Stop now asks whether to finish the chunk in hand or drop it, and shows how far in it is.
  It also takes effect straight away: the answer used to be read only between chunks, so Stop
  appeared to do nothing for up to ten minutes.
- Fixed: copies came out silent. Three places dropped the sound — the cut, the join, and the
  packing. The join had always discarded audio because it only ever joined masks, which have
  none; once it was used to join pictures too, that quietly became a bug.
- The projection and the packing are now **one pass instead of three**. Converting the picture to
  fisheye, converting the mask, and packing one into the other each used to write a file the size
  of the film and read it back — on a real job the picture conversion alone took 85 minutes and
  the folder carried 25 GB of intermediates. One filter graph never writes them; checked frame by
  frame against the old result, they differ by 0.8 of 255, which is the encoder rounding.
- That pass is done a frame range at a time, so it resumes per piece — and unlike matting these
  stages carry no state from frame to frame, so a range needs no run-up and can be done by any
  machine in any order.
- Every stage after the chunks can be resumed, not just the chunks: the joined mask, the cut
  picture, both fisheye conversions and the packing are each kept and skipped if already done.
  On a long film those steps are close to an hour, and a crash at the end used to cost all of it.
- Settings no longer change when the window is scrolled past them. Qt sends wheel events to
  whatever the pointer is over, focused or not — five notches over the lens angle turned 180 into
  155, which pointed a half-finished job at an empty folder and made every finished chunk look
  lost. Controls now ignore the wheel unless they have been clicked into.
- What a job was set to is written down beside its chunks, and starting it again with settings
  that would make those chunks useless asks first, naming exactly what changed. Settings that
  only affect the finished copy — the lens angle, the quality — are not counted, since they
  waste nothing.
- The folder holding a job's chunks is named after the source video, not the copy being made.
  It used to carry the lens angle, so nudging that spinner one notch pointed the job at an empty
  folder and every finished chunk vanished from view. Chunks in folders older versions used are
  brought over rather than matted again.
- Chunks are kept in one place — beside the video — however the work was shared out. They used
  to be moved into the shared job's own folder, which hid them from "Make one from what's done"
  and stranded them if the job was abandoned. Chunks left in the old place are brought back.
- A machine hands back any chunk it was still holding when it starts, instead of keeping it from
  every other machine until it falls silent for twenty minutes.
- **Make one from what's done** builds a watchable copy out of the chunks already matted, with the
  rest playing as the ordinary video — for looking at a job part-way without disturbing it.
- **Try the failed chunks again** puts every failed chunk back in the queue by hand, and the box
  below the shared folder now says which chunks failed and why.
- A video whose shares all fail is offered once more before being given up on, since they usually
  fail for a reason common to all of them.
- Switching a half-finished job to several machines keeps the chunks already matted: they are
  taken over from the working folder beside the video and only the rest is shared out.
- Fixed: work could be done twice over. A queue written earlier still listed pieces as waiting
  even after later attempts had finished them, and claiming handed them out without looking at
  the disk — so two machines ground through the same chunks from different queues. Claiming now
  checks whether the chunk already exists, and starting a video again takes down its previous
  queue instead of leaving both live.
- The final join is given to the machine with the best measured link rather than to whoever
  started the job — it is pure reading and writing, so on a wired machine it is minutes instead
  of a quarter of an hour. If that machine does not turn up within ten minutes, another takes it,
  so a job is never left unfinished because a computer was switched off.
- The last stage — projecting the mask and packing it into the picture — is shared between
  machines as well, not just the matting. It carries no state between frames, so its pieces need
  no run-up and can be small, which is what makes the machines balance themselves rather than
  having to be told which is slower. Only the final join stays on one machine.
- **Check this machine** times each computer's link to the shared folder and how fast it mattes,
  and publishes the result where the others can read it — one file per machine, no server. The
  panel shows them together and recommends which should start the job, choosing by link speed
  rather than graphics card, because the projecting and packing happen on the machine that posts
  it and move tens of gigabytes.
- While work is spread across machines, the window lists which machine has which chunk and how
  long since each last checked in, so a helper that has quietly died is visible rather than
  guessed at. It updates whichever tab is in front.
- Picking up a crashed job is quick now. Working out which chunks were already finished meant
  decoding each of them end to end — around 40 seconds per chunk locally and minutes over a
  network, so well over an hour on a long film before any work resumed. The count comes from the
  file header instead, and only a chunk with no header, which is one cut short by the crash, is
  counted the slow way.
- Restarting a job checks what is already done by reading each chunk's header instead of decoding
  it end to end. The old way took around forty seconds per chunk locally and minutes over a
  network — on a 37-chunk film, well over an hour before any work resumed.
- Matting runs **in chunks** — 5000 frames by default — joined at the end. A run that fails or is
  stopped keeps the chunks it finished, and starting again carries on from there instead of
  beginning over.
- BlueShelf now settles which encoder VRAutoMatte uses, by encoding a real frame at the real size.
  It was asking for libx264, which is GPL and deliberately absent from the bundled ffmpeg, and
  falling back to it whenever its own NVENC check timed out — which it does exactly when the card
  is busy, right after a long matte. H.264 could not have written these frames anyway: through
  NVENC it stops at 4096 pixels across.
- A **View** menu on each window: the library can show or hide the passthrough window and the
  other way round, so a job running for hours need not keep the library on screen.
- **Tools → Help another computer** (or starting with `--helper`) opens straight onto the helping
  tab, for a second machine that is only lending its graphics card.
- The passthrough window opens beside the library instead of over it, so a scene can be watched
  while deciding what to do with it. Videos can be dragged into it from the library or from a
  folder window, and closing it keeps the list, the scans and any job under way for the rest of
  the session.
- Scans are remembered against the file, together with any stretches marked by hand, so a video
  is never read twice. A file that has changed since is scanned again rather than trusted.
- One video can be spread across several computers through a folder they all share. Work is claimed
  by renaming a file, so nothing needs to be in charge and a machine can join or leave at any point;
  a share abandoned by a machine that was shut down goes back in the queue on its own.


## 0.1.0 — first release

**The library**

- Reads file and folder names to fill in performers, studios, release dates and categories, covering
  the common release naming styles, `organized/<performer>/` folders, and VR studio conventions.
- Grid and list views, search, sorting, favourites, ratings, play counts and resume points.
- Sidebar for performers, studios and categories, with counts.
- Mainstream films and TV episodes are detected and hidden by default.
- Hand edits are never overwritten by a later scan.

**Playing**

- Built on mpv, with hardware decoding and MPC-style keys.
- VR videos are detected and shown flat: 180°, 360° and fisheye, side-by-side or top/bottom, with
  drag to look around, scroll to zoom and eye swapping.
- Audio output device can be chosen, plus stereo downmix, seek step, autoplay next and resume.

**Housekeeping**

- Duplicate finder: identical copies by content, and the same scene re-encoded, by comparing frames.
- Review names: suggests merging performer spellings and removing titles mistaken for performers.
- Deleting moves files to a hidden trash on the same drive, with restore and empty.
- Cleanup mode: keep or delete one key at a time, with undo.
- Shortcut folders: a browsable tree where each video is a shortcut, so a scene with two performers
  appears under both without copying anything.
- Backup and restore of everything BlueShelf knows, plus a weekly automatic backup.

**Elsewhere**

- Watch on a Quest or phone: serves the library on your own network, with browsing by performer,
  playback, a VR mode, and deleting from the headset. Off by default and passcode protected, with a
  one-click Windows Firewall rule.
- The address ends in a word you choose - `192.168.1.169:8760/porn` - which stays in the address
  bar and carries the passcode, so nothing has to be typed twice and no cookie is needed. An
  address typed without the word says what to do instead of showing an error.
- The page is served over https, using a certificate BlueShelf makes for this PC. Browsers only offer
  VR on a secure page, so without this a headset has no VR mode at all. The first visit from each
  device shows a warning about the certificate, which has to be accepted once. Typing the plain
  address still works: it moves the browser across by itself.
- VR videos in the headset browser are drawn on a sphere instead of being cropped: in the page as a
  window you can drag to look around, and through the <b>VR</b> button as the real thing, one eye
  to each display, for 180°, 360° and fisheye in side-by-side or top/bottom.
- Controls inside VR: pull the trigger to bring up a panel and point at it for play, skip a video,
  jump 30 seconds, favourite, seek and delete - so a scene can be thrown out without taking the
  headset off. Delete still asks twice.
- DLNA sharing: BlueShelf shows up in DeoVR, Skybox, HereSphere, TVs, consoles, VLC and Kodi,
  organised by performer, studio, category and VR. VR scenes carry the title tags both DeoVR and
  Skybox read, so they open in the right projection. Off by default; hidden videos are never
  shared; only the local network can reach it.
- Better VR in the headset browser: hold the trigger on the progress bar and slide to scrub, or push
  the right stick sideways to run through the video. Grip and move to drag the picture; left stick
  turns and tilts, right stick zooms, B resets. A second row on the panel does the same for hand
  tracking. Adjustments are remembered per video. Fisheye videos no longer look too dark.
- DeoVR can browse the library directly, without DLNA: enter the address with /deovr on the end and
  DeoVR lists it by performer, with each scene's projection, eye layout, thumbnail and length already
  set, and a password step. Discovery is not involved, so it works on any network.
- New downloads appear by themselves: BlueShelf checks the library folders every few minutes (set in
  Settings, or off), and the headset page refreshes its own list, including the moment you come back
  to the tab. Neither interrupts a video or moves your place in the grid.
- Move a performer's videos into a folder: right-click a performer in the sidebar. Pick a folder you
  already made - BlueShelf offers ones whose names match - or a new one. The files themselves move,
  the library follows them, subtitles and artwork travel with them, and the whole batch can be put
  back with one button. Scenes with two performers are pointed out rather than filed quietly.
- Passthrough category: AR scenes with an alpha cut-out are detected from their names and grouped
  under Categories. DLNA and DeoVR are told, so their passthrough toggle appears by itself instead
  of the mask playing as a grey strip beside the picture.
- A Folders section in the sidebar: every library folder is listed on its own, with its own count, so
  each location can be browsed apart from the rest. It appears once there is more than one folder.
- A mute button on the library screen, so playback can be silenced before pressing play. It is
  remembered between videos and between sittings, and stays in step with the player's own button.
- Play counts record where each play happened - the desktop, the headset page, DeoVR or a DLNA
  player - counted once per sitting rather than once per request. Shown per video in the details
  panel and in total in the sharing window.
- The DeoVR feed is laid out for a headset: a few view tabs, then performers packed into tabs small
  enough for DeoVR to show whole, each scene saying who is in it. A tab per performer, or views
  only, can be chosen instead.
- Alpha passthrough works in DeoVR: it reads that flag from the file name in the link, so passthrough
  scenes are now linked as /video/57_alpha.mp4 and DeoVR offers alpha rather than only chroma key.
- Light and dark themes, following Windows by default.
