export default function App() {
  return (
    <div style={{
      minHeight: "100vh",
      background: "#0a0a0a",
      display: "flex",
      alignItems: "center",
      justifyContent: "center",
      fontFamily: "sans-serif",
      color: "#fff"
    }}>
      <div style={{ textAlign: "center" }}>
        <div style={{ fontSize: 64, marginBottom: 16 }}>🌿</div>
        <h1 style={{ fontSize: 28, fontWeight: 700, margin: "0 0 8px" }}>GREEN HOUSE</h1>
        <div style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 8,
          background: "#16a34a22",
          border: "1px solid #16a34a",
          borderRadius: 999,
          padding: "6px 16px",
          color: "#4ade80",
          fontSize: 14,
          fontWeight: 600
        }}>
          <span style={{
            width: 8, height: 8, borderRadius: "50%",
            background: "#4ade80",
            display: "inline-block",
            boxShadow: "0 0 8px #4ade80"
          }} />
          Bot Online
        </div>
      </div>
    </div>
  );
}
