/*
 * vdotool capture module.
 *
 * Loaded by the forked VDO.Ninja index.html when the URL is
 * ?vdotool=1&view=1&sessionId=<id>. Every CAPTURE_INTERVAL_MS, grab
 * the first playing remote <video>, draw it to an offscreen canvas,
 * encode JPEG, POST it to the writer sidecar at /vdotool/frame.
 *
 * This module is intentionally standalone and defensive: any failure
 * is swallowed so normal VDO.Ninja functionality is never disturbed.
 */
(function (globalScope) {
	'use strict';

	// JPEG snapshot cadence. The watcher in the Hermes plugin polls
	// the writer's output dir at its own interval and re-injects a
	// new frame to the agent every N seconds (configurable via
	// VDOTOOL_INJECT_INTERVAL_SECONDS). This value just controls how
	// fresh the "latest" file on disk is — keep it low enough that
	// whenever the watcher decides to inject, the disk frame is no
	// more than this many ms stale.
	var CAPTURE_INTERVAL_MS = 3000;
	var TARGET_WIDTH = 1280;
	var JPEG_QUALITY = 0.8;
	var WRITER_PATH = '/vdotool/frame';

	function log() {
		try {
			var args = Array.prototype.slice.call(arguments);
			args.unshift('[vdotool capture]');
			console.log.apply(console, args);
		} catch (_e) {}
	}

	function pickVideo() {
		var vids = document.querySelectorAll('video');
		for (var i = 0; i < vids.length; i++) {
			var v = vids[i];
			if (v && !v.paused && v.readyState >= 2 && v.videoWidth > 0 && v.videoHeight > 0) {
				return v;
			}
		}
		return null;
	}

	function makeCanvas(video) {
		var w = video.videoWidth;
		var h = video.videoHeight;
		if (w > TARGET_WIDTH) {
			h = Math.round(h * (TARGET_WIDTH / w));
			w = TARGET_WIDTH;
		}
		var canvas = document.createElement('canvas');
		canvas.width = w;
		canvas.height = h;
		return canvas;
	}

	function snapshotBlob(video) {
		return new Promise(function (resolve, reject) {
			try {
				var canvas = makeCanvas(video);
				var ctx = canvas.getContext('2d');
				ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
				canvas.toBlob(function (blob) {
					if (!blob) {
						reject(new Error('toBlob returned null'));
					} else {
						resolve(blob);
					}
				}, 'image/jpeg', JPEG_QUALITY);
			} catch (e) {
				reject(e);
			}
		});
	}

	function postFrame(sessionId, blob) {
		var url = WRITER_PATH + '?sessionId=' + encodeURIComponent(sessionId);
		var fd = new FormData();
		fd.append('frame', blob, 'frame.jpg');
		return fetch(url, {
			method: 'POST',
			body: fd,
			credentials: 'same-origin',
			mode: 'same-origin'
		});
	}

	function sessionIdValid(sid) {
		return typeof sid === 'string' && /^[A-Za-z0-9_-]{8,64}$/.test(sid);
	}

	globalScope.startVdotoolCapture = function startVdotoolCapture(sessionId) {
		if (!sessionIdValid(sessionId)) {
			log('invalid or missing sessionId, capture disabled');
			return;
		}
		log('waiting for remote video, sessionId=' + sessionId);

		var started = false;
		var pollTimer = setInterval(function () {
			if (started) return;
			var video = pickVideo();
			if (!video) return;
			started = true;
			clearInterval(pollTimer);
			log('remote video found (' + video.videoWidth + 'x' + video.videoHeight + '), starting capture every ' + CAPTURE_INTERVAL_MS + 'ms');
			tick(video);
			setInterval(function () { tick(video); }, CAPTURE_INTERVAL_MS);
		}, 500);

		function tick(video) {
			if (video.readyState < 2 || video.paused || video.videoWidth === 0) {
				var replacement = pickVideo();
				if (replacement) {
					video = replacement;
				} else {
					log('no active video this tick');
					return;
				}
			}
			snapshotBlob(video)
				.then(function (blob) { return postFrame(sessionId, blob); })
				.then(function (resp) {
					if (!resp.ok) {
						log('writer rejected frame: HTTP ' + resp.status);
					}
				})
				.catch(function (err) {
					log('capture failed:', err && err.message ? err.message : err);
				});
		}
	};
})(window);
