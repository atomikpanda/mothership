// Build-time regression check for golang/go#79717: a keepalive sent after
// connection shutdown must return an error, not spin forever on a closed channel.
package main

import (
	"crypto/ed25519"
	"crypto/rand"
	"fmt"
	"log"
	"net"
	"time"

	"golang.org/x/crypto/ssh"
)

func main() {
	done := make(chan error, 1)
	go func() { done <- checkClosedRequest() }()
	select {
	case err := <-done:
		if err != nil {
			log.Fatal(err)
		}
		fmt.Println("PASS: SSH keepalive after close returned an error without spinning")
	case <-time.After(10 * time.Second):
		log.Fatal("FAIL: SSH request did not finish; possible closed-channel CPU spin")
	}
}

func checkClosedRequest() error {
	_, key, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		return err
	}
	signer, err := ssh.NewSignerFromKey(key)
	if err != nil {
		return err
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return err
	}
	defer listener.Close()

	type accepted struct {
		conn *ssh.ServerConn
		err  error
	}
	server := make(chan accepted, 1)
	go func() {
		raw, err := listener.Accept()
		if err != nil {
			server <- accepted{err: err}
			return
		}
		config := &ssh.ServerConfig{NoClientAuth: true}
		config.AddHostKey(signer)
		conn, _, requests, err := ssh.NewServerConn(raw, config)
		if err != nil {
			raw.Close()
			server <- accepted{err: err}
			return
		}
		go ssh.DiscardRequests(requests)
		server <- accepted{conn: conn}
	}()

	client, err := ssh.Dial("tcp", listener.Addr().String(), &ssh.ClientConfig{
		User:            "regression-check",
		HostKeyCallback: ssh.FixedHostKey(signer.PublicKey()),
		Timeout:         5 * time.Second,
	})
	if err != nil {
		return err
	}
	defer client.Close()
	result := <-server
	if result.err != nil {
		return result.err
	}
	defer result.conn.Close()

	if _, _, err := result.conn.SendRequest("keepalive@sish", true, nil); err != nil {
		return fmt.Errorf("live keepalive failed: %w", err)
	}
	if err := result.conn.Close(); err != nil {
		return err
	}
	// Wait for mux.loop to close globalResponses: this is the production race's
	// terminal state, reached deterministically without timing sleeps.
	result.conn.Wait()
	if _, _, err := result.conn.SendRequest("keepalive@sish", true, nil); err == nil {
		return fmt.Errorf("closed SSH connection accepted a keepalive")
	}
	return nil
}
