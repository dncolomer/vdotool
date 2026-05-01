/*
 * vdotool speaker module.
 *
 * Loaded in the viewer page alongside capture.js when the URL is
 * ?vdotool=1&view=1&sessionId=<sid>. Its job is to play the agent's
 * TTS clips (MP3s in audio_out/<clip>.mp3, listed in
 * audio_out/queue.json) on the phone that's pushing camera into the
 * same VDO.Ninja room.
 *
 * Architecture:
 *   1. Create a synthetic audio MediaStream in the main viewer tab via
 *      a persistent AudioContext + MediaStreamAudioDestinationNode. TTS
 *      clips are decoded into AudioBuffers and played through this
 *      destination node; its outbound MediaStreamTrack becomes the
 *      agent's "microphone" track.
 *   2. Create a hidden iframe that hosts a second VDO.Ninja PUSHER in
 *      the same room (?push=<sid>_agent). The phone, already a peer
 *      in the same room, plays the audio from this pusher automatically
 *      via WebRTC — no extra phone-side setup needed.
 *   3. The iframe is built with `srcdoc` so we can inject an inline
 *      <script> that overrides `navigator.mediaDevices.getUserMedia`
 *      BEFORE any VDO.Ninja script runs, preventing a race where the
 *      iframe's VDO.Ninja call to getUserMedia beats our override and
 *      publishes a blank fake-device track.
 *   4. The inline override is installed in the iframe window, then the
 *      iframe redirects via `location.replace()` to the real viewer URL
 *      (./index.html?...) so we inherit same-origin + relative paths.
 *
 * Queue polling:
 *   - GET /vdotool/audio-queue?sessionId=<sid>
 *   - for each new pending clip: fetch /vdotool/audio/<sid>/<clip>,
 *     decodeAudioData, play on the destination node.
 *   - on ended: POST /vdotool/audio-ack?sessionId=...&clip=...
 *   - on `interrupt_epoch_ms` advance: stop current clip immediately.
 *
 * Defensive: every failure is caught and logged; we never break the
 * host viewer.
 */
(function (globalScope) {
	'use strict';

	var POLL_INTERVAL_MS = 1500;
	var WRITER_PATH_QUEUE = '/vdotool/audio-queue';
	var WRITER_PATH_CLIP = '/vdotool/audio/';
	var WRITER_PATH_ACK = '/vdotool/audio-ack';

	function log() {
		try {
			var args = Array.prototype.slice.call(arguments);
			args.unshift('[vdotool speaker]');
			console.log.apply(console, args);
		} catch (_e) {}
	}

	function sessionIdValid(sid) {
		return typeof sid === 'string' && /^[A-Za-z0-9_-]{8,64}$/.test(sid);
	}

	// ------------------------------------------------------------------
	// Companion pusher iframe.
	//
	// Architecture: the iframe loads VDO.Ninja's index.html with
	// ?vdotoolSpeaker=1, which triggers the head-injected bootstrap in
	// index.html (look for the "vdotool speaker bootstrap" block).
	// That bootstrap, running INSIDE the same page as VDO.Ninja's
	// main.js, builds an AudioContext + MediaStreamDestinationNode and
	// installs a getUserMedia override so VDO.Ninja's pusher gets the
	// synthetic stream as the "microphone". It also exposes
	// window.__vtPlay(buf, clipName) and window.__vtStop() so the
	// parent (this script) can hand off decoded clip bytes via a
	// direct same-origin function call (no postMessage hop, no
	// MediaStream cross-realm transfer — those don't work in Chromium).
	//
	// The bootstrap posts {type:'vdotool/speaker-ready'} once it's
	// done; the parent waits for that before calling __vtPlay.
	// ------------------------------------------------------------------

	function spawnPusherIframe(roomId, agentStreamId, sessionId, origin) {
		var iframe = document.createElement('iframe');
		iframe.style.width = '1px';
		iframe.style.height = '1px';
		iframe.style.position = 'fixed';
		iframe.style.left = '-1000px';
		iframe.style.top = '-1000px';
		iframe.style.opacity = '0';
		iframe.allow = 'autoplay; microphone; camera';

		// Direct navigation, no srcdoc bootstrap. The receiving page is
		// VDO.Ninja's index.html with the head-injected speaker hook.
		// &lanonly forces local-only ICE so the pusher-side peer
		// connection comes up without depending on STUN/TURN
		// reachability — both peers are on the same LAN by design.
		iframe.src = origin + '/?room=' + encodeURIComponent(roomId)
			+ '&push=' + encodeURIComponent(agentStreamId)
			+ '&autostart=1'
			+ '&cleanoutput=1'
			+ '&videodevice=0'
			+ '&audiodevice=1'
			+ '&noaudioprocessing=1'
			+ '&vdotoolSpeaker=1'
			+ '&lanonly';
		return iframe;
	}

	// ------------------------------------------------------------------
	// Audio-queue polling loop.
	// ------------------------------------------------------------------

	function speaker(sessionId, iframe) {
		// All audio playback happens inside the iframe (the
		// MediaStreamDestinationNode lives there so VDO.Ninja's
		// gUM-override can hand it directly to its pusher). The parent
		// only ferries clip bytes via iframe.contentWindow.__vtPlay.
		var playedClips = Object.create(null);
		var lastInterruptEpoch = 0;

		function fetchQueue() {
			var url = WRITER_PATH_QUEUE + '?sessionId=' + encodeURIComponent(sessionId);
			return fetch(url, { credentials: 'same-origin' })
				.then(function (r) {
					if (!r.ok) throw new Error('queue fetch HTTP ' + r.status);
					return r.json();
				});
		}

		function fetchClip(clipName) {
			var url = WRITER_PATH_CLIP + encodeURIComponent(sessionId) + '/' + encodeURIComponent(clipName);
			return fetch(url, { credentials: 'same-origin' })
				.then(function (r) {
					if (!r.ok) throw new Error('clip fetch HTTP ' + r.status);
					return r.arrayBuffer();
				});
		}

		function ackClip(clipName) {
			var url = WRITER_PATH_ACK + '?sessionId=' + encodeURIComponent(sessionId)
				+ '&clip=' + encodeURIComponent(clipName);
			return fetch(url, { method: 'POST', credentials: 'same-origin' })
				.catch(function (e) { log('ack failed', e); });
		}

		function iframePlay(buf, clipName) {
			// Same-origin iframe → cross-window function call works.
			// __vtPlay is installed by buildIframeSrcdoc above.
			try {
				var w = iframe.contentWindow;
				if (w && typeof w.__vtPlay === 'function') {
					return w.__vtPlay(buf, clipName);
				}
			} catch (e) {
				log('iframePlay failed', e);
			}
			return Promise.resolve();
		}

		function iframeStop() {
			try {
				var w = iframe.contentWindow;
				if (w && typeof w.__vtStop === 'function') {
					w.__vtStop();
				}
			} catch (_e) {}
		}

		var busy = false;
		async function tick() {
			if (busy) return;
			busy = true;
			try {
				var q = await fetchQueue();
				if (q && typeof q.interrupt_epoch_ms === 'number' && q.interrupt_epoch_ms > lastInterruptEpoch) {
					lastInterruptEpoch = q.interrupt_epoch_ms;
					log('interrupt epoch advanced -> aborting current clip');
					iframeStop();
				}
				var pending = (q && Array.isArray(q.pending)) ? q.pending : [];
				for (var i = 0; i < pending.length; i++) {
					var entry = pending[i];
					if (!entry || !entry.clip) continue;
					if (playedClips[entry.clip]) continue;
					playedClips[entry.clip] = true;
					log('handing clip to iframe', entry.clip, 'text=', JSON.stringify(entry.text || '').slice(0, 60));
					try {
						var bytes = await fetchClip(entry.clip);
						await iframePlay(bytes, entry.clip);
					} catch (e) {
						log('clip playback failed', entry.clip, e);
					}
					await ackClip(entry.clip);
				}
			} catch (e) {
				log('tick error', e);
			} finally {
				busy = false;
			}
		}
		return { tick: tick };
	}

	// ------------------------------------------------------------------
	// Public entry point.
	// ------------------------------------------------------------------

	globalScope.startVdotoolSpeaker = function startVdotoolSpeaker(sessionId) {
		if (!sessionIdValid(sessionId)) {
			log('invalid sessionId, speaker disabled');
			return;
		}

		fetch('/vdotool/healthz', { credentials: 'same-origin' })
			.then(function (r) { return r.json(); })
			.then(function (h) {
				if (!h || h.tts === false) {
					log('TTS disabled by writer (healthz says so); speaker not starting');
					return;
				}
				beginSpeaker(sessionId);
			})
			.catch(function (e) {
				log('healthz probe failed; starting speaker anyway', e);
				beginSpeaker(sessionId);
			});
	};

	function beginSpeaker(sessionId) {
		var params = new URLSearchParams(location.search);
		var roomId = params.get('room') || '';
		if (!roomId) {
			log('no room param in viewer URL; cannot publish audio');
			return;
		}
		var agentStreamId = 'vt_' + sessionId + '_agent';

		var iframe = spawnPusherIframe(roomId, agentStreamId, sessionId, location.origin);

		// The iframe will load VDO.Ninja's index.html with
		// ?vdotoolSpeaker=1, which runs the head-injected bootstrap to
		// install __vtPlay and the gUM-override. Once ready, the
		// bootstrap posts {type:'vdotool/speaker-ready'} to us.
		// We don't strictly need to wait for that to start polling
		// the writer queue — fetchClip is slow enough that __vtPlay
		// has time to install — but logging it confirms the flow.
		var sawReady = false;
		function onMsg(ev) {
			if (!ev.data) return;
			if (ev.data.type === 'vdotool/speaker-ready') {
				if (!sawReady) {
					sawReady = true;
					log('speaker page bootstrap ready (gUM override installed in iframe)');
				}
			}
		}
		window.addEventListener('message', onMsg);

		document.body.appendChild(iframe);
		log('spawned pusher iframe; room=' + roomId + ' agent_stream=' + agentStreamId);

		var sp = speaker(sessionId, iframe);
		sp.tick();
		setInterval(sp.tick, POLL_INTERVAL_MS);
	}
})(window);
