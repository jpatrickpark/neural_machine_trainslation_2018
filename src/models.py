import torch
import torch.nn as nn
from torch import optim
import torch.nn.functional as F
import config
from torch.nn.utils.rnn import pad_packed_sequence, pack_padded_sequence

class RnnEncoder(nn.Module):
    
    def __init__(self, args, padding_idx, src_vocab_size):
        super(RnnEncoder, self).__init__()
        self.args = args
        self.num_directions = 2 if args.bidirectional else 1

        self.embedding = nn.Embedding(
            src_vocab_size, 
            args.embedding_size, 
            padding_idx = padding_idx
        )
        
        self.rnn_type = args.rnn_type
        
        self.rnn = config.RNN_TYPES[args.rnn_type](
            input_size = args.embedding_size, 
            hidden_size = args.hidden_size,
            num_layers = args.num_encoder_layers,
            dropout = args.dropout,
            bidirectional = args.bidirectional
        )
            
    def forward(self, hidden, cell_state, x, lengths):
        #print("src shape", x.shape) # torch.Size([32, 16])
        # dimenstion of x: (seq_len, batch, input_size)
        x = self.embedding(x)
        #print("embedded shape", x.shape) # torch.Size([32, 16, 256])
        # dimension of x after embedding: (seq_len, batch, embedding_size)
        
        x = pack_padded_sequence(x, lengths)
        if self.rnn_type == 'lstm':
            x, (hidden, cell_state) = self.rnn(x, (hidden, cell_state))
        else:
            x, hidden = self.rnn(x, hidden)
        x, output_lengths = pad_packed_sequence(x)
        #print("after encoder shape", x.shape) # torch.Size([32, 16, 128])
        # dimension of x after encoder: (seq_len, batch, hidden_size)
        #print("encoder hidden shape", self.hidden.shape) # torch.Size([1, 16, 128])
        if self.num_directions == 2:
            x = x[:, :, :self.args.hidden_size] + x[:, : ,self.args.hidden_size:]
        return x, hidden, cell_state
    
    def random_init_hidden(self, device, current_batch_size):
        # This is only needed for encoder, 
        # since decoder's first hidden state is the output of encoder
        # LSTM: output, (h_n, c_n)
        # rnn, gru: output, h_n
        
        # we declare initial hidden tensor every time because the last batch size
        # might be different from the rest of the batch size,
        # but feel free to modify this if you have a better idea.
        hidden = torch.zeros(
            self.args.num_encoder_layers * self.num_directions, 
            current_batch_size, 
            self.args.hidden_size, 
            device=device
        )
        
        # https://r2rt.com/non-zero-initial-states-for-recurrent-neural-networks.html
        nn.init.xavier_normal_(hidden)
        
        cell_state = None

        if self.rnn_type == 'lstm':
            cell_state = torch.zeros(
                self.args.num_encoder_layers * self.num_directions, 
                current_batch_size, 
                self.args.hidden_size, 
                device=device
            )
            nn.init.xavier_normal_(cell_state)

        return hidden, cell_state
    

class CnnEncoder(nn.Module):
    
    def __init__(self, args, padding_idx, src_vocab_size):
                    
        super(CnnEncoder, self).__init__()
        
        self.position_embedding = nn.Embedding(args.max_sentence_length, args.embedding_size)
        self.word_embedding = nn.Embedding(src_vocab_size, args.embedding_size, padding_idx = padding_idx)
        self.dropout = args.dropout

        self.conv = nn.ModuleList([nn.Conv1d(args.hidden_size, args.hidden_size, args.kernel_size,
                                      padding=args.kernel_size // 2) for _ in range(args.num_encoder_layers)])

    def forward(self, x, position_ids):
        
        # Get position and word embeddings 
        position_embed = self.position_embedding(position_ids)
        word_embed = self.word_embedding(x)
        
        # Apply dropout to the sum of position + word embeddings
        embed = F.dropout(position_embed + word_embed, self.dropout, self.training)
        
        # Successive application of convolution layers followed by residual connection and non-linearity        
        output = embed.transpose(0,1).transpose(1,2)

        for i, layer in enumerate(self.conv):
          # layer(output) is the conv operation, after which we add the original output creating a residual connection
            output = torch.tanh(layer(output)+output)        

        return output.transpose(1,2).transpose(0,1)
    
    
class RnnDecoder(nn.Module):
    def __init__(self, args, padding_idx, trg_vocab_size):
        super(RnnDecoder, self).__init__()
        self.args = args
        self.n_layers = args.num_decoder_layers
        self.relu = args.relu
        
        self.embedding = nn.Embedding(
            trg_vocab_size, 
            args.embedding_size, 
            padding_idx = padding_idx
        )
        
        # Use only one layer of RNN in decoder for now
        self.rnn = config.RNN_TYPES[self.args.rnn_type](
            input_size = args.embedding_size, 
            hidden_size = args.hidden_size,
            dropout = args.dropout,
            num_layers = args.num_decoder_layers,
        )

        self.rnn_type = self.args.rnn_type
        
        self.linear = nn.Linear(args.hidden_size, trg_vocab_size)

    def forward(self, hidden, cell_state, x):
        #print("trg shape", x.shape) torch.Size([40, 16])
        x = self.embedding(x)
        #print("embedded shape", x.shape) # torch.Size([40, 16, 256])
        # If we pass SOS token here, and run it iterative fashion, then it's translation
        # thus we need to set maximum sequence length
        # if we pass the entire training data here, then it's using teacher forcing.
        # TODO: Do we use relu here?
        if self.relu:
            x = F.relu(x)
        if self.rnn_type == 'lstm':
            x, (hidden, cell_state) = self.rnn(x, (hidden, cell_state))
        else:
            x, hidden = self.rnn(x, hidden)
        #print("after decoder shape", x.shape) # torch.Size([40, 16, 128])
        #print("decoder hidden shape", self.hidden.shape) # torch.Size([1, 16, 128])
        x = self.linear(x)
        #print("after linear shape", x.shape) # torch.Size([40, 16, 5679])
        return x, hidden, cell_state


class Attn(nn.Module):
    '''
    Code modified from lab8
    '''
    def __init__(self, method, hidden_size):
        super(Attn, self).__init__()
        
        self.method = method
        self.hidden_size = hidden_size
        
        if self.method == 'general':
            self.attn = nn.Linear(self.hidden_size, hidden_size)

        elif self.method == 'concat':
            self.attn = nn.Linear(self.hidden_size * 2, hidden_size)
            self.v = nn.Parameter(torch.FloatTensor(1, hidden_size))

    def forward(self, hidden, encoder_outputs_a, encoder_outputs_c=None, gaussian_multiplier=None):
        '''
        Return 
            context_vector = size B x 1 x hidden_size'''
        if encoder_outputs_c is None:
            encoder_outputs_c = encoder_outputs_a
        # Create variable to store attention energies
        energy = self.score(hidden, encoder_outputs_a)
        score = F.softmax(energy, dim = 1).view(1, self.batch_size, -1) # works, but bad code!
        if gaussian_multiplier is not None:
            assert self.batch_size == 1
            score = score * gaussian_multiplier.view(1,1,-1)
        context_vector = torch.bmm(score.transpose(1,0), encoder_outputs_c.transpose(1,0))
        #print(self.method, context_vector.shape)
        return context_vector, score
    
    def score(self, hidden, encoder_output):
        '''
        Args
            hidden: size 1 x B x hidden_size
            encoder_output: size N x B x hidden_size
        Return 
            energy: size B x N x 1
        '''
        self.batch_size = hidden.shape[1]
        if self.method == 'dot':
            energy = torch.bmm(encoder_output.transpose(1,0), hidden.squeeze(0).unsqueeze(2)) 
            return energy 
        
        elif self.method == 'general':
            energy = torch.bmm(encoder_output.transpose(1,0), self.attn(hidden.squeeze(0)).unsqueeze(2)) 
            
            return energy
        
        elif self.method == 'concat':
            concat = torch.cat((hidden.transpose(1,0).expand(self.batch_size ,encoder_output.shape[0], self.hidden_size), encoder_output.transpose(1,0)), dim = 2)
            tanh = nn.Tanh()
            out = tanh(self.attn(concat)) #size: B x N x hidden_size
            energy = torch.bmm(self.v.expand(self.batch_size, 1, self.hidden_size), out.transpose(2,1)).transpose(1,2)
            return energy
        else:
            raise NotImplementedError()
        
        
        
class LuongAttnDecoderRNN(nn.Module):
    def __init__(self, args, trg_padding_idx, output_size, device=None):
        super(LuongAttnDecoderRNN, self).__init__()

        # Keep for reference
        self.attn_model = args.attn_model
        self.rnn_type = args.rnn_type
        self.hidden_size = args.hidden_size
        self.output_size = output_size
        self.n_layers = args.num_decoder_layers
        self.dropout = args.dropout
        self.relu = args.relu
        self.embedding_size = args.embedding_size
        self.local_p = args.local_p
        self.device = device
        if self.local_p:
            self.window_size = args.window_size
        
        # Define layers
        self.embedding = nn.Embedding(
            self.output_size, 
            args.embedding_size, 
            padding_idx=trg_padding_idx
        )
        self.embedding_dropout = nn.Dropout(self.dropout)
        self.gru = config.RNN_TYPES[args.rnn_type](
            input_size = args.embedding_size, 
            hidden_size = args.hidden_size,
            num_layers = self.n_layers, 
            dropout=self.dropout
        )
        self.concat = nn.Linear(self.hidden_size * 2, self.hidden_size)
        self.out = nn.Linear(self.hidden_size, self.output_size)
        
        # Choose attention model
        assert self.attn_model in ["dot", "general", "concat"]
        self.attn = Attn(self.attn_model, self.hidden_size)

        if self.local_p:
            assert device is not None
            self.Wp = nn.Linear(self.hidden_size, self.hidden_size)
            self.vp = torch.FloatTensor(self.hidden_size).to(device)
        

    def forward(self, hidden, cell_state, input_seq, encoder_outputs_a, encoder_outputs_c=None, src_lengths=None):
        # Note: we run this one step at a time
        if encoder_outputs_c is None:
            encoder_outputs_c = encoder_outputs_a

        # Get the embedding of the current input word (last output word)
        batch_size = input_seq.size(0)
        embedded = self.embedding(input_seq)
        embedded = self.embedding_dropout(embedded)
        embedded = embedded.view(1, batch_size, self.embedding_size) # S=1 x B x N

        # Get current hidden state from input word and last hidden state
        
        if self.relu:
            embedded = F.relu(embedded)
        if self.rnn_type == 'lstm':
            rnn_output, (hidden, cell_state) = self.gru(embedded, (hidden, cell_state))
        else:
            rnn_output, hidden = self.gru(embedded, hidden)

        #print("vp expand shape", self.vp.expand_as(rnn_output).shape)
        #testing = torch.mm(self.vp.expand_as(rnn_output),rnn_output) # what we want
        #testing = self.vp * rnn_output #elementwise multiplication
        #print("is vp cuda",self.vp.is_cuda)
        #print("is rnn_output cuda", rnn_output.is_cuda)
        #print(testing.shape)

        if self.local_p:
            print(self.vp.shape, rnn_output.shape, encoder_outputs_a.shape)
            # p_t for the entire batch
            intermediate = torch.mm(F.tanh(self.Wp(rnn_output.squeeze(0))), self.vp.unsqueeze(1)).squeeze(1) # what we want
            print(intermediate.shape)
            #intermediate = F.sigmoid(torch.bmm(self.vp, F.tanh(self.Wp(rnn_output))))
            src_lengths_float = src_lengths.float()
            p_t = F.sigmoid(intermediate) * src_lengths_float# JP: this length contains sos and eos, and don't do anything weird for window size. Use this number to get the window in such a way that the gradient will flow, but not necessary, because it will also flow through the gaussian multiplier
            print(p_t)
            context, attn_weights = [], []
            seq_len, batch_size = encoder_outputs_a.shape[:2]
            gaussian_multiplier = -(torch.FloatTensor(range(seq_len)).to(self.device).unsqueeze(-1).expand(seq_len, batch_size) - p_t.unsqueeze(0).expand(seq_len, batch_size)).to(self.device).pow(2)/(self.window_size**2/2)
            print("gaussian shape", gaussian_multiplier.shape)
            for i, each in enumerate(torch.floor(p_t).long()): # not sure if I can iterate over tensor
                start = each - self.window_size
                end = each + self.window_size
                if start < 0:
                    start = 0 # could be a problem since start becomes an integer and not a tensor
                if end > src_lengths[i]:
                    end = src_lengths[i].item()
                assert end - start >= 2, "This should never happen, since src_lengths >= 2"
                print("start, end", start, end)
                #gaussian_multiplier = -(torch.FloatTensor(range(end-start)).to(self.device) - p_t[i]).to(self.device).pow(2)/(self.window_size**2/2)
                #print("gaussian shape", gaussian_multiplier.shape)
                #if end - start == 1:
                    # Handle case where the window size is 1 (This should never happen, but just in case)
                    #current_context, current_attn_weights = self.attn(rnn_output, encoder_outputs_a[start].unsqueeze(0), encoder_outputs_c[start].unsqueeze(0))
                #else:
                current_context, current_attn_weights = self.attn(
                    rnn_output[:,i,:].unsqueeze(1), 
                    encoder_outputs_a[start:end,i,:].unsqueeze(1),
                    encoder_outputs_c[start:end,i,:].unsqueeze(1),
                    gaussian_multiplier[start:end,i]
                )
                # generate gaussian vector
                context.append(current_context)
                attn_weights.append(current_attn_weights)
                print("current context shape",current_context.shape)
            context = torch.cat(context) #dim=0 is actually correct
            #print(context.shape)


        else:
            # Calculate attention from current RNN state and all encoder outputs;
            # apply to encoder outputs to get weighted average
            context, attn_weights = self.attn(rnn_output, encoder_outputs_a, encoder_outputs_c)
            #print(context.shape)

        # Attentional vector using the RNN hidden state and context vector
        # concatenated together (Luong eq. 5)
        rnn_output = rnn_output.squeeze(0) # S=1 x B x N -> B x N
        context = context.squeeze(1)       # B x S=1 x N -> B x N
        concat_input = torch.cat((rnn_output, context), 1)
        concat_output = F.tanh(self.concat(concat_input))

        # Finally predict next token (Luong eq. 6, without softmax)
        output = self.out(concat_output)

        # Return final output, hidden state, and attention weights (for visualization)
        #print("decoder output", output.shape)
        return output, attn_weights, hidden, cell_state
    
    def random_init_hidden(self, device, current_batch_size):
        # This is needed when using CNN encoder, 
        # since CNN encoder does not return hidden state
        # LSTM: output, (h_n, c_n)
        # rnn, gru: output, h_n
        
        # we declare initial hidden tensor every time because the last batch size
        # might be different from the rest of the batch size,
        # but feel free to modify this if you have a better idea.
        hidden = torch.zeros(
            self.n_layers, 
            current_batch_size, 
            self.hidden_size, 
            device=device
        )
        
        # https://r2rt.com/non-zero-initial-states-for-recurrent-neural-networks.html
        nn.init.xavier_normal_(hidden)

        cell_state = None
        
        if self.rnn_type == 'lstm':
            cell_state = torch.zeros(
                self.n_layers, 
                current_batch_size, 
                self.hidden_size, 
                device=device
            )
            nn.init.xavier_normal_(cell_state)
        
        return hidden, cell_state

    
    
class AttnRnnDecoder(nn.Module):
    def __init__(self, args, padding_idx, trg_vocab_size, max_sent_length):
        super(RnnDecoder, self).__init__()
        self.args = args

        self.embedding = nn.Embedding(trg_vocab_size, args.embedding_size, padding_idx = padding_idx)
        
        # Use only one layer of RNN in decoder for now
        self.rnn = config.RNN_TYPES[self.args.rnn_type](input_size = args.embedding_size, hidden_size = args.hidden_size, dropout = args.dropout)
        

        self.attn_layer  = nn.Linear(args.hidden_size, args.hidden_size, bias=True)

        self.out = nn.Linear(args.hidden_size, trg_vocab_size)
        
    def forward(self, x, prev_hidden, encoder_output):
        '''
        Args:
            encoder_output: hidden state output from encoder; size (hidden_size, max_sent_len)
        '''
        x = self.embedding(x)
        attn_part1 = self.attn_layer(prev_hidden)
        attn_weights = F.softmax(torch.matmul(attn_part1, encoder_out), dim = 1)
        attn_combined = torch.matmul(attn_weights, encoder_out.transpose(1,0))
        attn_combined = F.relu(attn_combined)
        x, self.hidden = self.rnn(x, attn_combined) 
        x = F.relu(x)
        x = self.linear(x)

        return x
    
class RnnEncoderDecoder(nn.Module):
    # I thought this will be nicer to use a big model that has both encoder and decoder
    # But I am not sure if this is actually feasible 
    # since we have to reuse output of the decoder as input to decoder,
    # it might be better if decoder is a separate unit.
    def __init__(self, args, padding_idx, src_vocab_size, trg_vocab_size):
        super(RnnEncoderDecoder, self).__init__()
        self.args = args

        self.encoder = RnnEncoder(args, padding_idx, src_vocab_size)
        self.decoder = RnnDecoder(args, padding_idx, trg_vocab_size)
        
        #self.encoder.random_init_hidden()
        
    def forward(self, src, trg):
        print(src.shape)
        print(trg.shape)
        self.encoder(src) # we do not use output of encoder
        output = self.decoder(trg)
        return output
