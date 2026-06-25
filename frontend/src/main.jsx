import React from 'react';
import ReactDOM from 'react-dom/client';
import './index.css';
import App from './App.jsx';

// When Module 2 ships: add BrowserRouter + Routes here.
// App.jsx becomes a layout shell; current BOL state moves to pages/BolReconciliation.jsx.
ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
