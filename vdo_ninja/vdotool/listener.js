/*
 * vdotool listener module.
 *
 * Loaded in the viewer page alongside capture.js and speaker.js when
 * the URL is ?vdotool=1&view=1&sessionId=<sid>. Its job is to capture
 * spoken utterances from the user's push side (the phone), encode
 * them as WebM/Opus chunks, and POST each utterance to the writer at
 * /vdotool/audio-in so the Hermes plugin can transcribe and inject
 * them into the conversation as user messages.
 *
 * Pipeline:
 *   1. Find the incoming remote audio MediaStreamTrack attached to the
 *      first playing remote <video> — that's where VDO.Ninja puts the
 *      push-side's mic by default.
 *   2. Wire it through a Web Audio AnalyserNode to measure dBFS.
 *   3. Poll GET /vdotool/listener-mute for the current mute-window
 *      deadline. The plugin writes audio_out/muted_until_ms.txt
 *      whenever it queues a TTS clip, because the phone plays that
 *      clip through its speaker and its mic would pick it up — echo
 *      loop. Discard any utterance that started recording before the
 *      window ends.
 *   4. VAD: when volume crosses THRESHOLD for MIN_START_MS AND we're
 *      not in a mute window, start a MediaRecorder.
 *   5. When volume stays below THRESHOLD for MIN_SILENCE_MS, stop
 *      the recorder and POST the Blob.
 *   6. Watch the audio track for 'ended' / 'mute' events and POST
 *      /vdotool/listener-status so the agent can be told.
 *
 * Defensive: all failures are swallowed and surfaced via
 * listener-status when we can't do our job.
 */
(function (globalScope) {
	'use strict';

	var WRITER_PATH_IN = '/vdotool/audio-in';
	var WRITER_PATH_MUTE = '/vdotool/listener-mute';
	var WRITER_PATH_STATUS = '/vdotool/listener-status';

	function envOrDefault(paramName, fallback) {
		var p = new URLSearchParams(location.search);
		var v = p.get(paramName);
		if (v === null || v === undefined || v === '') return fallback;
		var n = Number(v);
		return Number.isFinite(n) ? n : fallback;
	}
	var THRESHOLD_DB = envOrDefault('vtVadThresholdDb', -45);
	var MIN_START_MS = envOrDefault('vtVadMinStartMs', 150);
	var MIN_SILENCE_MS = envOrDefault('vtVadMinSilenceMs', 700);
	var MIN_UTTERANCE_MS = envOrDefault('vtVadMinUtteranceMs', 300);
	var MAX_UTTERANCE_MS = envOrDefault('vtVadMaxUtteranceMs', 15000);
	var MUTE_POLL_INTERVAL_MS = envOrDefault('vtMutePollIntervalMs', 1000);

	function log() {
		try {
			var args = Array.prototype.slice.call(arguments);
			args.unshift('[vdotool listener]');
			console.log.apply(console, args);
		} catch (_e) {}
	}

	function sessionIdValid(sid) {
		return typeof sid === 'string' && /^[A-Za-z0-9_-]{8,64}$/.test(sid);
	}

	function pickRemoteAudioTrack() {
		var vids = document.querySelectorAll('video');
		for (var i = 0; i < vids.length; i++) {
			var v = vids[i];
			var stream = v && v.srcObject;
			if (!stream || typeof stream.getAudioTracks !== 'function') continue;
			var tracks = stream.getAudioTracks();
			for (var j = 0; j < tracks.length; j++) {
				if (tracks[j].enabled && tracks[j].readyState === 'live') {
					return { track: tracks[j], stream: stream };
				}
			}
		}
		return null;
	}

	function supportedMimeType() {
		var candidates = [
			'audio/webm;codecs=opus',
			'audio/ogg;codecs=opus',
			'audio/webm',
			'audio/ogg',
		];
		if (typeof MediaRecorder === 'undefined' || typeof MediaRecorder.isTypeSupported !== 'function') {
			return '';
		}
		for (var i = 0; i < candidates.length; i++) {
			if (MediaRecorder.isTypeSupported(candidates[i])) return candidates[i];
		}
		return '';
	}

	function postUtterance(sessionId, blob, mimeType) {
		var url = WRITER_PATH_IN + '?sessionId=' + encodeURIComponent(sessionId);
		return fetch(url, {
			method: 'POST',
			body: blob,
			headers: { 'Content-Type': mimeType || blob.type || 'audio/webm' },
			credentials: 'same-origin',
			mode: 'same-origin',
		}).catch(function (e) { log('POST failed', e); });
	}

	function postStatus(sessionId, ok, reason) {
		try {
			var url = WRITER_PATH_STATUS + '?sessionId=' + encodeURIComponent(sessionId)
				+ '&ok=' + (ok ? 'true' : 'false')
				+ '&reason=' + encodeURIComponent(reason || '');
			return fetch(url, {
				method: 'POST',
				credentials: 'same-origin',
				mode: 'same-origin',
			}).catch(function (e) { log('status post failed', e); });
		} catch (_e) {}
	}

	function fetchMuteUntil(sessionId) {
		var url = WRITER_PATH_MUTE + '?sessionId=' + encodeURIComponent(sessionId);
		return fetch(url, { credentials: 'same-origin' })
			.then(function (r) {
				if (!r.ok) throw new Error('mute HTTP ' + r.status);
				return r.json();
			})
			.then(function (j) {
				return {
					muted_until_ms: (j && typeof j.muted_until_ms === 'number') ? j.muted_until_ms : 0,
					now_ms: (j && typeof j.now_ms === 'number') ? j.now_ms : Date.now(),
				};
			})
			.catch(function () { return { muted_until_ms: 0, now_ms: Date.now() }; });
	}

	globalScope.startVdotoolListener = function startVdotoolListener(sessionId) {
		if (!sessionIdValid(sessionId)) {
			log('invalid sessionId, listener disabled');
			return;
		}

		fetch('/vdotool/healthz', { credentials: 'same-origin' })
			.then(function (r) { return r.json(); })
			.then(function (h) {
				if (h && h.stt === false) {
					log('STT disabled by writer (healthz says so); listener not starting');
					return;
				}
				beginListener(sessionId);
			})
			.catch(function () { beginListener(sessionId); });
	};

	function beginListener(sessionId) {
		log('waiting for remote audio, sessionId=' + sessionId);
		var mime = supportedMimeType();
		if (!mime) {
			log('MediaRecorder with Opus not supported in this browser');
			postStatus(sessionId, false, 'mediarecorder_unsupported');
			return;
		}
		log('using recorder MIME type:', mime);

		var started = false;
		var pollTimer = setInterval(function () {
			if (started) return;
			var picked = pickRemoteAudioTrack();
			if (!picked) return;
			started = true;
			clearInterval(pollTimer);
			log('remote audio track found; starting VAD loop');
			postStatus(sessionId, true, 'track_found');
			runVadLoop(sessionId, picked.track, mime);
		}, 500);
	}

	function runVadLoop(sessionId, audioTrack, mime) {
		var ctx = new (window.AudioContext || window.webkitAudioContext)();
		if (ctx.state === 'suspended') {
			ctx.resume().catch(function () {});
		}

		var analyseStream = new MediaStream([audioTrack]);
		var source = ctx.createMediaStreamSource(analyseStream);
		var analyser = ctx.createAnalyser();
		analyser.fftSize = 1024;
		analyser.smoothingTimeConstant = 0.2;
		source.connect(analyser);

		audioTrack.addEventListener('ended', function () {
			log('audio track ended; listener going silent');
			postStatus(sessionId, false, 'track_ended');
		});
		audioTrack.addEventListener('mute', function () {
			log('audio track muted');
			postStatus(sessionId, false, 'track_muted');
		});
		audioTrack.addEventListener('unmute', function () {
			log('audio track unmuted');
			postStatus(sessionId, true, 'track_unmuted');
		});

		var mutedUntilMs = 0;
		function refreshMute() {
			fetchMuteUntil(sessionId).then(function (r) {
				var skew = r.now_ms - Date.now();
				mutedUntilMs = Math.max(0, r.muted_until_ms - skew);
			});
		}
		refreshMute();
		setInterval(refreshMute, MUTE_POLL_INTERVAL_MS);

		var buf = new Float32Array(analyser.fftSize);

		function levelDb() {
			analyser.getFloatTimeDomainData(buf);
			var rms = 0;
			for (var i = 0; i < buf.length; i++) {
				rms += buf[i] * buf[i];
			}
			rms = Math.sqrt(rms / buf.length) || 1e-12;
			return 20 * Math.log10(rms);
		}

		var recorder = null;
		var chunks = [];
		var recStartedAt = 0;
		var voiceStartMs = 0;
		var lastVoiceMs = 0;

		function startRecording() {
			try {
				recorder = new MediaRecorder(analyseStream, { mimeType: mime });
			} catch (e) {
				log('MediaRecorder construction failed', e);
				postStatus(sessionId, false, 'recorder_construct_failed');
				return;
			}
			chunks = [];
			recorder.ondataavailable = function (ev) {
				if (ev.data && ev.data.size > 0) chunks.push(ev.data);
			};
			recorder.onstop = function () {
				var blob = new Blob(chunks, { type: mime });
				var dur = Date.now() - recStartedAt;
				if (Date.now() < mutedUntilMs) {
					log('dropping utterance (' + dur + 'ms) — overlapped with TTS mute window');
				} else if (dur < MIN_UTTERANCE_MS || blob.size === 0) {
					log('utterance too short (' + dur + 'ms / ' + blob.size + 'B), dropping');
				} else {
					log('utterance ended, dur=' + dur + 'ms bytes=' + blob.size + ', POSTing');
					postUtterance(sessionId, blob, mime);
				}
				recorder = null;
				chunks = [];
			};
			recStartedAt = Date.now();
			lastVoiceMs = recStartedAt;
			try {
				recorder.start(250);
				log('recording started');
			} catch (e) {
				log('recorder.start failed', e);
				recorder = null;
			}
		}

		function stopRecording() {
			if (!recorder) return;
			try { recorder.stop(); } catch (_e) {}
		}

		setInterval(function () {
			var now = Date.now();
			var db = levelDb();
			var loud = db > THRESHOLD_DB;
			var inMuteWindow = now < mutedUntilMs;

			if (!recorder) {
				if (loud && !inMuteWindow) {
					if (!voiceStartMs) voiceStartMs = now;
					if (now - voiceStartMs >= MIN_START_MS) {
						startRecording();
						voiceStartMs = 0;
					}
				} else {
					voiceStartMs = 0;
				}
			} else {
				if (loud) {
					lastVoiceMs = now;
				}
				var silentFor = now - lastVoiceMs;
				var totalFor = now - recStartedAt;
				if (silentFor >= MIN_SILENCE_MS) {
					stopRecording();
				} else if (totalFor >= MAX_UTTERANCE_MS) {
					log('utterance reached max length, force-stopping');
					stopRecording();
				}
			}
		}, 50);
	}
})(window);
