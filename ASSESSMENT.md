# PERS PoC: feasibility assessment (Milestone 4, 2026-11-04)

## Verdict

The software side is feasible. A single CPU core with no GPU turns a 1080p
camera into a privacy-preserving avatar that shows standing, sitting and lying,
about one second behind real life, without any video reaching the viewer.
What is not yet good enough for a safety product is posture judgment in hard
cases (a curled-up body, a partly visible body, motion in the dark) and the
consumer camera's reliability. Both are solvable, but they are the work, not
the plumbing.

## Measurements

All on the PoC setup: Wyze Cam Pan at `HD120` (1920x1080), bridge on a Mac on
the home LAN, SSH tunnel to a 1 vCPU / 3.8 GB Hostinger VPS, YOLOv8n at 640 px
every 4th frame with ByteTrack, MediaPipe Pose lite.

| Metric | Result |
|---|---|
| Delay, camera to VPS | 0.44 to 1.03 s (camera's on-screen clock vs. arrival time, 3 frames at 1080p; 0.5 to 1.7 s at 360p in Milestone 1) |
| Delay, frame on VPS to browser | 53 to 115 ms |
| Delay, total camera to avatar | about 0.5 to 1.2 s |
| Frame rate, empty room | 14.5 to 15 fps processed, 0 skipped |
| Frame rate, person moving | 9 to 15 fps, about 11 to 12 on average. The camera itself sends fewer frames when there is motion; the service keeps up with everything it gets |
| CPU, whole VPS | 39% with the room empty, 43 to 51% with a person in view |
| Memory, pose service | 600 MB |
| Per frame | YOLO 70 to 100 ms per run (every 4th frame), pose 12 to 20 ms, frame in to packet out 40 to 70 ms |

## Posture accuracy by condition

Each row was checked against snapshots taken every 5 s during the test.

| Condition | Result |
|---|---|
| Standing, whole body visible, daylight | correct; brief false "sitting" while walking or turning (under 1 s) |
| Sitting in a chair, on the couch, on the floor | correct, within about 1 s |
| Lying on the couch, stretched out or on the side | correct, within about 3 s |
| Lying on the floor in open view | correct after the person is found; about 8 s with no detection while getting down |
| Lying curled up on the couch | **wrong**: read as sitting or standing for the whole 15 s |
| Lying flat behind the couch back | **missed**: no person detected (Milestone 2) |
| Body mostly out of the picture (lying at the frame edge) | no detection for about 5 s, then lying |
| Only head and shoulders visible, close to the camera | **wrong**: kept saying "standing" (the last posture), because legs are needed to decide |
| Night vision (infrared), still postures | correct: lying on the couch, sitting in a chair |
| Night vision, moving | pose found in only 26 to 87% of frames because of motion blur; 76% overall vs. 90 to 100% in daylight |
| Two people | **not tested**: no second consenting adult was available. During an earlier run another person walked into view; the tracker counted them and stayed on whoever it was following, but it picks the largest box when it loses track, so it has no idea *who* it is watching |

## What this means for the product

1. **Posture needs a better judge than two angles.** The rule set (torso tilt,
   thigh fold, box shape) handles clear postures but fails on a curled-up body,
   which is exactly what a person often looks like after a fall. Next step: a
   small classifier trained on keypoint sequences (not single frames), with an
   explicit "can't tell" output instead of repeating the last posture.
2. **Camera placement matters as much as the model.** Furniture that hides the
   floor (the couch back) and a frame edge that cuts off the floor create blind
   spots. Installation guidance, or a second camera, should guarantee the floor
   is visible.
3. **Detection gaps of 5 to 8 s happen during the very movements that matter**
   (getting down to the floor). Fall logic must treat "person vanished low in
   the frame" as a signal, not as "no one there".
4. **Night needs its own tuning.** Infrared images blur when the person moves.
   Expect to need a camera with a faster night shutter or a model fine-tuned on
   infrared frames.
5. **Multiple people need identity.** The tracker keeps an ID while it can see
   someone, but cannot tell the resident from a visitor or a child. The product
   needs a way to know who to watch (enrolled body shape, a wearable tag, or
   zones) without face recognition, and the two-person test still has to be run.
6. **Consumer cameras are the weak link.** The Wyze camera dropped its session
   twice in two days (about 1 minute to reconnect each time), and when there is
   motion it delivers about 11 fps while still stamping frames as 15 fps. The
   product's LAN-only cameras need a health watchdog and must be measured by
   wall clock.
7. **Compute is not the problem.** One camera at 1080p used about half of one
   VPS core. An in-home hub with a modern 4-core ARM chip should handle two or
   three cameras, but this must be measured on the actual hub hardware.

## Not covered by this PoC

- Two or more people (no consenting second adult available).
- Real falls, elderly movement patterns, assistive devices (walkers, wheelchairs).
- Long-running stability (hours or days), multiple cameras, hub hardware.
- Everything in the README's "PoC shortcuts that MUST NOT ship" list still
  applies: cloud processing, Wyze cloud, no viewer auth, one hardcoded avatar.
