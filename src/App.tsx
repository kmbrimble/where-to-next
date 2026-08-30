function App() {
  return (
    <div data-testid="app-shell">
      <header>
        <button data-testid="home-button" aria-label="Home" type="button">
          Where to Next
        </button>
        <span data-testid="staleness-badge">Data as of --:--</span>
      </header>
      <main data-testid="main-content" />
    </div>
  )
}

export default App
