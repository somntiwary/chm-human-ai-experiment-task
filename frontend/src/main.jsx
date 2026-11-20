import { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import './style.css';

const tokenKey = 'decision-study-token';
const joinKey = 'decision-study-join';
const electionRuleLabels = {
  total_votes: 'Most votes across both rounds',
  round_two_votes: 'Most votes in Round 2',
  votes_from_others: 'Most votes from other players across both rounds',
  random_draw: 'Equal-chance draw among the remaining tied players',
};

function Countdown({ deadline, serverTime, receivedAt }) {
  const [now, setNow] = useState(() => performance.now());

  useEffect(() => {
    const interval = window.setInterval(() => setNow(performance.now()), 200);
    return () => window.clearInterval(interval);
  }, []);

  const remaining = Date.parse(deadline) - Date.parse(serverTime) + receivedAt - Math.max(now, receivedAt);
  const seconds = Math.max(0, Math.ceil(remaining / 1000));
  const timestamp = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
  return (
    <div className="countdown">
      <span>Time left</span>
      <time dateTime={`PT${seconds}S`}>{timestamp}</time>
    </div>
  );
}

function Chat({ players, messages }) {
  const log = useRef(null);
  useEffect(() => {
    log.current.scrollTop = log.current.scrollHeight;
  }, [messages]);

  return (
    <section aria-labelledby="chat-title" className="chat">
      <h3 id="chat-title">Round chat</h3>
      <div className="chat-log" role="log" aria-labelledby="chat-title" ref={log}>
        {messages.length === 0 && <p className="muted">No messages yet.</p>}
        <ul>
          {messages.map(({ id, sender, text }) => (
            <li key={id}>
              <strong>{players.find(({ slot }) => slot === sender).label}</strong>
              <p>{text}</p>
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}

function MessageForm({ round, connected, send }) {
  const [text, setText] = useState('');

  function submit(event) {
    event.preventDefault();
    const message = text.trim();
    if (message && send({ type: 'message', round, text: message })) setText('');
  }

  return (
    <form className="message-form" onSubmit={submit}>
      <label htmlFor="message">Message</label>
      <div className="message-input">
        <input id="message" value={text} onChange={(event) => setText(event.target.value)}
          maxLength={500} autoComplete="off" placeholder="Write a message…" disabled={!connected} />
        <button type="submit" disabled={!connected || !text.trim()}>Send</button>
      </div>
    </form>
  );
}

function VoteForm({ state, connected, send }) {
  const [candidate, setCandidate] = useState('');

  function submit(event) {
    event.preventDefault();
    if (candidate) send({ type: 'ballot', round: state.round, candidate });
  }

  if (state.has_voted) {
    return <p className="notice" role="status">Vote recorded. Waiting for the round to finish.</p>;
  }

  return (
    <form className="vote-form" onSubmit={submit}>
      <fieldset disabled={!connected}>
        <legend>Who should be the Decision Maker?</legend>
        <div className="candidates">
          {state.players.map(({ slot, label }) => (
            <label key={slot}>
              <input type="radio" name="candidate" value={slot} checked={candidate === slot}
                onChange={() => setCandidate(slot)} />
              {label}
            </label>
          ))}
        </div>
        <button type="submit" disabled={!candidate}>Submit vote</button>
      </fieldset>
    </form>
  );
}

function ElectionRules() {
  return (
    <section aria-labelledby="rules-heading" className="election-rules">
      <h3 id="rules-heading">How the Decision Maker is chosen</h3>
      <ol>
        <li>Keep the players with the most votes across both rounds.</li>
        <li>If tied, keep those with the most votes in Round 2.</li>
        <li>If still tied, keep those with the most votes from other players across both rounds.</li>
        <li>Draw from any players still tied, with an equal chance for each.</li>
      </ol>
      <p>Self-votes count in the first two steps.</p>
    </section>
  );
}

function Results({ players, result }) {
  const winner = players.find(({ slot }) => slot === result.winner);
  return (
    <section aria-labelledby="results-heading">
      <h2 id="results-heading">Session complete</h2>
      <p className="result-winner"><strong>{winner.label}</strong> is the Decision Maker.</p>
      <dl className="decision-rule">
        <dt>Deciding rule</dt>
        <dd>{electionRuleLabels[result.rule]}</dd>
      </dl>
      <table className="vote-totals">
        <caption>Final vote totals</caption>
        <thead>
          <tr><th scope="col">Player</th><th scope="col">Votes across both rounds</th></tr>
        </thead>
        <tbody>
          {players.map(({ slot, label }) => (
            <tr key={slot}>
              <th scope="row">{label}</th>
              <td>{result.totals[slot]}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function Round({ state, connected, send }) {
  const discussion = state.phase === 'discussion';
  return (
    <>
      <div className="round-heading">
        <h2>Round {state.round} {discussion ? 'discussion' : 'voting'}</h2>
        {discussion && <Countdown deadline={state.deadline} serverTime={state.server_time}
          receivedAt={state.receivedAt} />}
      </div>
      {state.round === 1 && !state.has_voted && <ElectionRules />}
      {!discussion && <VoteForm state={state} connected={connected} send={send} />}
      <Chat players={state.players} messages={state.messages} />
      {discussion && <MessageForm round={state.round} connected={connected} send={send} />}
    </>
  );
}

function RoundBreak({ state, connected, send }) {
  const onBreak = state.deadline !== null;
  return (
    <section aria-labelledby="break-heading">
      <div className="round-heading">
        <h2 id="break-heading">Round 1 discussion and voting complete</h2>
        {onBreak && <Countdown deadline={state.deadline} serverTime={state.server_time}
          receivedAt={state.receivedAt} />}
      </div>
      {onBreak ? (
        <p>Take a 10-second break.</p>
      ) : state.round_2_ready ? (
        <p className="notice" role="status">You’re ready. Waiting for Round 2 to start.</p>
      ) : (
        <>
          <p>Join when you’re ready. Round 2 starts when everyone is ready.</p>
          <button type="button" disabled={!connected} onClick={() => send({ type: 'ready', round: 1 })}>
            Join Round 2
          </button>
        </>
      )}
    </section>
  );
}

function App() {
  const [token, setToken] = useState(() => sessionStorage.getItem(tokenKey));
  const [state, setState] = useState(null);
  const [connected, setConnected] = useState(false);
  const [joining, setJoining] = useState(false);
  const [notice, setNotice] = useState('');
  const socket = useRef(null);

  useEffect(() => {
    if (!token) return;

    let retry;
    let handshake;

    function connect() {
      const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws';
      const connection = new WebSocket(`${scheme}://${window.location.host}/api/ws`);
      socket.current = connection;
      handshake = window.setTimeout(() => connection.close(), 8_000);
      connection.onopen = () => connection.send(JSON.stringify({ token }));
      connection.onmessage = ({ data }) => {
        const update = JSON.parse(data);
        if (update.type === 'state') {
          window.clearTimeout(handshake);
          setConnected(true);
          setState({ ...update, receivedAt: performance.now() });
          setNotice('');
        } else if (update.type === 'error') {
          setNotice(update.message);
        }
      };
      connection.onclose = ({ code }) => {
        window.clearTimeout(handshake);
        setConnected(false);
        if (code === 4401) {
          sessionStorage.removeItem(tokenKey);
          setToken(null);
          setState(null);
          setNotice('Your player could not be recovered. Join the study again.');
        } else {
          retry = window.setTimeout(connect, 1_000);
        }
      };
    }

    connect();
    return () => {
      window.clearTimeout(retry);
      window.clearTimeout(handshake);
      const connection = socket.current;
      connection.onopen = connection.onmessage = connection.onclose = null;
      connection.close();
      socket.current = null;
    };
  }, [token]);

  async function joinStudy() {
    setJoining(true);
    setNotice('');
    try {
      const key = sessionStorage.getItem(joinKey) ?? crypto.randomUUID();
      sessionStorage.setItem(joinKey, key);
      const response = await fetch('/api/join', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ join_key: key }),
        signal: AbortSignal.timeout(8_000),
      });
      const result = await response.json();
      if (!response.ok) {
        if (response.status === 410) sessionStorage.removeItem(joinKey);
        setNotice(typeof result.detail === 'string' ? result.detail : 'Could not join the study.');
        return;
      }

      sessionStorage.setItem(tokenKey, result.token);
      sessionStorage.removeItem(joinKey);
      setToken(result.token);
    } catch {
      setNotice('Could not connect to the study. Try again.');
    } finally {
      setJoining(false);
    }
  }

  function send(action) {
    if (!connected || socket.current?.readyState !== WebSocket.OPEN) return false;
    setNotice('');
    socket.current.send(JSON.stringify(action));
    return true;
  }

  const player = state?.players.find(({ slot }) => slot === state.you);
  return (
    <main className="study">
      <header className="study-heading">
        <h1>Decision Study</h1>
        {player && <p className="player-identity">You are <strong>{player.label}</strong></p>}
      </header>
      <div className="study-panel">
        {!token && (
          <section className="entry" aria-labelledby="join-heading">
            <h2 id="join-heading">Join the study</h2>
            <p>Discuss who should be the Decision Maker, then vote at the end of each round.</p>
            <button type="button" onClick={joinStudy} disabled={joining}>
              {joining ? 'Joining…' : 'Join study'}
            </button>
          </section>
        )}
        {token && !connected && (
          <p className="notice" role="status">{state ? 'Reconnecting…' : 'Connecting to the study…'}</p>
        )}
        {state?.phase === 'waiting' && (
          <section className="entry" aria-labelledby="waiting-heading">
            <h2 id="waiting-heading">Waiting room</h2>
            <p>The discussion begins when the group is ready.</p>
          </section>
        )}
        {(state?.phase === 'discussion' || state?.phase === 'voting') && (
          <Round key={state.round} state={state} connected={connected} send={send} />
        )}
        {state?.phase === 'break' && (
          <RoundBreak state={state} connected={connected} send={send} />
        )}
        {state?.phase === 'results' && (
          <Results players={state.players} result={state.result} />
        )}
        {notice && <p className="error" role="alert">{notice}</p>}
      </div>
    </main>
  );
}

createRoot(document.getElementById('root')).render(<App />);
